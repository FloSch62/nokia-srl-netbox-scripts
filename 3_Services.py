#!/opt/netbox/venv/bin/python
if __name__ == "__main__":
    import os
    import sys
    import django
    sys.path.append('/opt/netbox/netbox')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
    django.setup()

import yaml
import re
import random
import itertools
from extras.scripts import (
    AbortScript,
    ChoiceVar,
    FileVar,
    IntegerVar,
    IPAddressWithMaskVar,
    MultiObjectVar,
    ObjectVar,
    Script,
    StringVar,
)
from ipam.models import RouteTarget
from extras.models import (
    CustomFieldChoiceSet,
    Tag,
)
try:
    from ipam.models import L2VPN
except ImportError:
    from vpn.models import L2VPN
from tenancy.models import Tenant
from dcim.models import (
    Device,
    Interface,
    Location,
)
from ipam.models import (
    IPAddress,
    VRF,
)
from django.utils.text import slugify as django_slugify
from contextlib import suppress


# From: https://github.com/netbox-community/netbox/discussions/12315#discussioncomment-5685891
def slugify(model, name, chars=50):
    base = str(name)
    base = re.sub(r'[^-.\w\s]', '', base)        # Remove unneeded chars
    base = re.sub(r'^[\s.]+|[\s.]+$', '', base)  # Trim leading/trailing spaces
    base = re.sub(r'[-.\s]+', '-', base)         # Convert spaces and decimals to hyphens
    base = base.lower()                          # Convert to lowercase
    slug = base[0:chars]                         # Trim to first chars
    for i in range(5):
        if model.objects.filter(slug=slug).count() > 0:
            slug = "%s-%06x" % (base[0:chars-7], random.randrange(0, 0x1000000))
        else:
            return slug
    else:
        raise AbortScript("It's not your lucky day - unable to create a unique slug")


commissioning_state_choices = []
with suppress(CustomFieldChoiceSet.DoesNotExist):
    commissioning_state_choices = CustomFieldChoiceSet.objects.get(name="Service_commissioning_state").choices


class L2VPNsBulkImport(Script):
    class Meta:
        name = "Bulk import L2VPNs"
        description = "Create or update L2VPNs based on YAML input"

        field_order = ['yamlfile']

    yamlfile = FileVar(
        description="Upload YAML file for the setup",
    )

    def process_l2vpn(self, l2vpn_data, commit):

        # Check for Location existence
        if 'location' in l2vpn_data:
            try:
                location = Location.objects.get(name=l2vpn_data['location'])
                location_pk = location.pk
            except Location.DoesNotExist:
                self.log_failure(f"Location '{l2vpn_data['location']}' not found. Cannot create/update L2VPN '{l2vpn_data['name']}' without a valid location.")
                return
        else:
            location_pk = None

        # Ensure Tenant exists or create it
        tenant = None
        if l2vpn_data.get('tenant'):
            tenant, _ = Tenant.objects.get_or_create(name=l2vpn_data['tenant'], defaults={'slug': slugify(Tenant, l2vpn_data['tenant'])})

        # Ensure L2VPN exists or create it
        defaults = {'slug': slugify(L2VPN, l2vpn_data['name']), 'type': 'vpls', 'identifier': l2vpn_data['identifier']}
        l2vpn, created = L2VPN.objects.get_or_create(
            name=l2vpn_data['name'],
            defaults=defaults
        )
        if not created:
            for k, v in defaults.items():
                if k != 'slug':
                    setattr(l2vpn, k, v)
        action = "Created" if created else "Updated"

        self.log_success(f"{action} L2VPN '{l2vpn.name}' with identifier '{l2vpn_data['identifier']}'")

        # Process RouteTargets
        import_rt, _ = RouteTarget.objects.get_or_create(name=l2vpn_data['import_target'])
        export_rt, _ = RouteTarget.objects.get_or_create(name=l2vpn_data['export_target'])
        if commit:
            l2vpn.import_targets.add(import_rt)
            l2vpn.export_targets.add(export_rt)
        self.log_info(f"Associated import/export RouteTargets with L2VPN '{l2vpn.name}'")

        # VLAN
        l2vpn.custom_field_data['L2vpn_vlan'] = str(l2vpn_data['vlan']) if l2vpn_data['vlan'] > 0 else "untagged"
        self.log_info(f"Set VLAN '{l2vpn_data['vlan']}' for L2VPN '{l2vpn.name}'")

        if location_pk:
            l2vpn.custom_field_data['Service_location'] = location_pk
            self.log_info(f"Set location '{location.name}' for L2VPN '{l2vpn.name}'")

        if tenant:
            l2vpn.tenant = tenant
            self.log_info(f"Set tenant '{tenant.name}' for L2VPN '{l2vpn.name}'")

        # Commissioning_state
        if 'commissioning_state' in l2vpn_data:
            l2vpn.custom_field_data['Commissioning_state'] = l2vpn_data['commissioning_state']

        # VRF (IPVRF)
        if l2vpn_data.get('ipvrf'):
            vrf, vrf_created = VRF.objects.get_or_create(name=l2vpn_data['ipvrf'])
            l2vpn.custom_field_data['L2vpn_ipvrf'] = vrf.pk
            vrf_action = "Created" if vrf_created else "Found"
            self.log_info(f"{vrf_action} VRF '{vrf.name}' for L2VPN '{l2vpn.name}'")

        # IPVRF Gateway IP Address
        if l2vpn_data.get('ipvrf_gateway'):
            ip_address, ip_created = IPAddress.objects.get_or_create(address=l2vpn_data['ipvrf_gateway'])
            l2vpn.custom_field_data['L2vpn_gateway'] = ip_address.pk
            ip_action = "Created" if ip_created else "Found"
            self.log_info(f"{ip_action} IP address '{ip_address.address}' for L2VPN '{l2vpn.name}'")

        if commit:
            l2vpn.save()
            self.log_info(f"Saved custom field updates for L2VPN '{l2vpn.name}'")

        # Process devices and their interfaces
        tag_name = f"l2vpn:{l2vpn.name}"
        itf_tag, created = Tag.objects.get_or_create(name=tag_name, defaults={'slug': slugify(Tag, tag_name)})
        if created:
            self.log_success(f"Created new tag '{tag_name}' for interfaces associated with L2VPN '{l2vpn.name}'.")
        else:
            self.log_info(f"Found existing tag '{tag_name}' for interfaces associated with L2VPN '{l2vpn.name}'.")

        # Tag interfaces listed in data
        for device_entry in l2vpn_data.get('devices', []):
            device_name = device_entry['device_name']
            try:
                device = Device.objects.get(name=device_name)
            except Device.DoesNotExist:
                self.log_failure(f"Device '{device_name}' not found. Cannot associate interfaces for L2VPN '{l2vpn.name}'.")
                continue  # Skip to the next device_entry

            for interface_name in device_entry.get('interfaces', []):
                try:
                    interface = Interface.objects.get(name=interface_name, device=device)
                    interface.tags.add(itf_tag)
                    interface.save()

                    self.log_info(f"Tagged interface '{interface_name}' on device '{device_name}' with L2VPN '{l2vpn.name}'.")
                except Interface.DoesNotExist:
                    self.log_failure(f"Interface '{interface_name}' on device '{device_name}' not found. Cannot complete association for L2VPN '{l2vpn.name}'.")

        # Remove tag from interfaces not listed in data
        device_iface_map = {entry['device_name']: entry.get('interfaces', []) for entry in l2vpn_data.get('devices', []) if 'device_name' in entry}
        for iface in Interface.objects.filter(tags=itf_tag):
            if iface.device.name in device_iface_map:
                if iface.name not in device_iface_map[iface.device.name]:
                    iface.tags.remove(itf_tag)
                    iface.save()
                    self.log_warning(f"Disassociating '{iface.device.name} {iface.name}' from L2VPN '{l2vpn.name}'.")
            else:
                iface.tags.remove(itf_tag)
                iface.save()
                self.log_warning(f"Disassociating '{iface.device.name} {iface.name}' from L2VPN '{l2vpn.name}'.")

    # Method to parse YAML input
    def parse_yaml(self, yaml_input):
        return yaml.safe_load(yaml_input)

    # Main method to run the script
    def run(self, data, commit):
        # Assuming 'data' contains the YAML content
        yaml_content = self.parse_yaml(data['yamlfile'].read().decode('utf-8'))

        for l2vpn_data in yaml_content['l2vpns']:
            self.process_l2vpn(l2vpn_data, commit)


class CreateL2VPN(Script):
    class Meta:
        name = "Create L2VPN (mac-vrf)"
        description = "Create or update a single L2VPN instance based on provided inputs."

    mac_vrf_id = IntegerVar(description="MAC VRF ID")
    description = StringVar(description="Description", required=False)
    tenant = ObjectVar(model=Tenant, description="Tenant", required=False, query_params={"name__isw": "svc:"})
    location = ObjectVar(model=Location, description="Location")
    device = ObjectVar(model=Device, description="This is a filter for the interfaces", query_params={"location": "$location"}, required=False)
    interfaces = MultiObjectVar(model=Interface, description="Interfaces", query_params={"device_id": "$device"})
    vlan = IntegerVar(description="VLAN ID, 0 for untagged", min_value=0, max_value=4095)
    route_target = StringVar(description="Route Target", required=False, regex=re.compile(r'^(?:\d+:\d+)?$'))
    ipvrf_gateway = IPAddressWithMaskVar(description="Gateway Address", required=False)

    def run(self, data, commit):
        # Extract form data
        mac_vrf_id = data['mac_vrf_id']
        description = data.get('description', '')
        tenant = data.get('tenant')
        location = data['location']
        interfaces = data['interfaces']
        vlan = data['vlan']
        ipvrf_gateway = data.get('ipvrf_gateway')
        rt = data.get('route_target')

        # Prepare Route Target values
        import_target = rt if rt else f"100:{mac_vrf_id}"
        export_target = rt if rt else f"100:{mac_vrf_id}"

        # Prepare mac vrf name django_slugify(<location>)-macvrf-macvrfid
        mac_vrf_name = f"{django_slugify(location.name)}-macvrf-{mac_vrf_id}"

        # Create or update L2VPN instance
        defaults = {'slug': slugify(L2VPN, mac_vrf_name), 'type': 'vpls', 'identifier': mac_vrf_id, 'description': description}
        l2vpn, created = L2VPN.objects.get_or_create(
            name=mac_vrf_name,
            defaults=defaults
        )
        if not created:
            for k, v in defaults.items():
                if k != 'slug':
                    setattr(l2vpn, k, v)
        self.log_success(f"{'Created' if created else 'Updated'} L2VPN '{l2vpn.name}'.")

        # Process Route Targets
        import_rt, _ = RouteTarget.objects.get_or_create(name=import_target)
        export_rt, _ = RouteTarget.objects.get_or_create(name=export_target)
        l2vpn.import_targets.set([import_rt])
        l2vpn.export_targets.set([export_rt])
        self.log_success(f"Associated import/export Route Targets with L2VPN '{l2vpn.name}'.")

        # VLAN
        l2vpn.custom_field_data['L2vpn_vlan'] = str(vlan) if vlan > 0 else "untagged"
        self.log_info(f"Set VLAN '{vlan}' for L2VPN '{l2vpn.name}'")

        # Location
        l2vpn.custom_field_data['Service_location'] = location.pk
        self.log_info(f"Set location '{location.name}' for L2VPN '{l2vpn.name}'")

        # Tenant
        if tenant:
            l2vpn.tenant = tenant
            self.log_info(f"Set tenant '{tenant.name}' for L2VPN '{l2vpn.name}'")

        # Commissioning_state
        l2vpn.custom_field_data['Commissioning_state'] = 'Planned'

        # Process IPVRF Gateway if provided
        if ipvrf_gateway:
            ipvrf_gateway_obj, ip_created = IPAddress.objects.get_or_create(
                address=str(ipvrf_gateway),
                defaults={'description': f"Gateway for L2VPN {l2vpn.name}"}
            )
            l2vpn.custom_field_data['L2vpn_gateway'] = ipvrf_gateway_obj.pk
            self.log_success(f"{'Created' if ip_created else 'Updated'} IPvRF gateway IP {ipvrf_gateway_obj.address}.")

        # Tag and process interfaces
        tag_name = f"l2vpn:{l2vpn.name}"
        interface_tag, _ = Tag.objects.get_or_create(name=tag_name, defaults={'slug': slugify(Tag, tag_name)})
        for interface in interfaces:
            # Assume a tag is created for each L2VPN to associate interfaces
            interface.tags.add(interface_tag)
            interface.save()
            self.log_success(f"Tagged interface '{interface}' with L2VPN '{l2vpn.name}'.")

        if commit:
            l2vpn.save()
            self.log_success("All changes have been committed.")

        return "L2VPN setup complete."


class DeleteL2VPN(Script):
    class Meta:
        name = "Delete L2VPN (mac-vrf)"
        description = "Safely delete a selected L2VPN instance and its associated resources."

    # Allows user to select an L2VPN instance to delete
    l2vpn = ObjectVar(
        model=L2VPN,
        description="Select the L2VPN instance to delete",
    )

    def run(self, data, commit):
        l2vpn_instance = data['l2vpn']

        # Log the operation
        self.log_info(f"Deleting L2VPN '{l2vpn_instance.name}' and its associated resources.")

        # Delete associated RouteTargets if necessary
        rt_deletion_candidates = {rt: 0 for rt in itertools.chain(l2vpn_instance.import_targets.all(), l2vpn_instance.export_targets.all())}
        l2vpns = L2VPN.objects.prefetch_related('import_targets', 'export_targets').all()
        vrfs = VRF.objects.prefetch_related('import_targets', 'export_targets').all()
        for l2vpn in l2vpns:
            if l2vpn == l2vpn_instance:
                continue
            for rt in l2vpn.import_targets.all():
                if rt in rt_deletion_candidates:
                    rt_deletion_candidates[rt] += 1
            for rt in l2vpn.export_targets.all():
                if rt in rt_deletion_candidates:
                    rt_deletion_candidates[rt] += 1
        for vrf in vrfs:
            for rt in vrf.import_targets.all():
                if rt in rt_deletion_candidates:
                    rt_deletion_candidates[rt] += 1
            for rt in vrf.export_targets.all():
                if rt in rt_deletion_candidates:
                    rt_deletion_candidates[rt] += 1
        for rt in rt_deletion_candidates:
            if rt_deletion_candidates[rt] == 0:
                rt.delete()
                self.log_success(f"Deleted RouteTarget '{rt.name}' associated with L2VPN '{l2vpn_instance.name}'.")

        # if l2vpn has a gateway IP address, delete it
        if l2vpn_instance.custom_field_data.get('L2vpn_gateway'):
            gateway_ip = IPAddress.objects.get(pk=l2vpn_instance.custom_field_data['L2vpn_gateway'])
            gateway_ip.delete()
            self.log_success(f"Deleted gateway IP '{gateway_ip.address}'.")

        tag_name = f"l2vpn:{l2vpn_instance.name}"
        try:
            tag = Tag.objects.get(name=tag_name)
            tag.delete()
            self.log_success(f"Deleted Tag '{tag_name}'.")
        except Tag.DoesNotExist:
            self.log_info(f"No Tag '{tag_name}' found. Skipping tag deletion.")

        # Finally, delete the L2VPN instance itself
        if commit:
            l2vpn_instance_name = l2vpn_instance.name  # Store name for logging after deletion
            l2vpn_instance.delete()
            self.log_success(f"Deleted L2VPN '{l2vpn_instance_name}'.")

        return "L2VPN deletion process complete."


class VRFsBulkImport(Script):
    class Meta:
        name = "Bulk import VRFs"
        description = "Create or update VRFs based on YAML input"
        field_order = ['yamlfile']

    yamlfile = FileVar(description="Upload YAML file for the setup")

    def process_vrf(self, vrf_data, commit):
        # Check and get location if specified
        location = None
        if vrf_data['location']:
            try:
                location = Location.objects.get(name=vrf_data['location'])
            except Location.DoesNotExist:
                self.log_failure(f"Location '{vrf_data['location']}' not found. Cannot proceed with VRF '{vrf_data['name']}'")
                return

        # Ensure Tenant exists or create it
        tenant = None
        if vrf_data['tenant']:
            tenant, _ = Tenant.objects.get_or_create(name=vrf_data['tenant'], defaults={'slug': slugify(Tenant, vrf_data['tenant'])})

        # Ensure VRF exists or create it, including RD if specified
        defaults = {
            'tenant': tenant,
        }
        vrf, created = VRF.objects.update_or_create(
            name=vrf_data['name'],
            defaults=defaults
        )
        action = "Created" if created else "Updated"
        self.log_success(f"{action} VRF '{vrf.name}' with Identfier '{vrf_data.get('identifier', '')}'")

        # Process RouteTargets
        if vrf_data['import_target']:
            import_rt, _ = RouteTarget.objects.get_or_create(name=vrf_data['import_target'])
            if commit:
                vrf.import_targets.add(import_rt)
        if vrf_data['export_target']:
            export_rt, _ = RouteTarget.objects.get_or_create(name=vrf_data['export_target'])
            if commit:
                vrf.export_targets.add(export_rt)

        self.log_info(f"Updated import/export RouteTargets for VRF '{vrf.name}'")

        # Handle custom fields and relations

        if vrf_data['identifier']:
            vrf.custom_field_data['Vrf_identifier'] = vrf_data['identifier']

        if location:
            # Assuming 'Service_location' is the field name for Location in your custom fields
            vrf.custom_field_data['Service_location'] = location.pk

        if 'commissioning_state' in vrf_data:
            vrf.custom_field_data['Commissioning_state'] = vrf_data['commissioning_state']

        if vrf_data['wan_vrf']:
            try:
                wan_vrf = VRF.objects.get(name=vrf_data['wan_vrf'])
                # Assuming 'Vrf_wanvrf' is the field name for WAN VRF relation in your custom fields
                vrf.custom_field_data['Vrf_wanvrf'] = wan_vrf.pk
            except VRF.DoesNotExist:
                self.log_failure(f"WAN VRF '{vrf_data['wan_vrf']}' not found. Cannot set WAN VRF for '{vrf_data['name']}'")
                return

        if commit:
            vrf.save()
            self.log_info(f"Saved updates for VRF '{vrf.name}'")

    def parse_yaml(self, yaml_input):
        return yaml.safe_load(yaml_input)

    def run(self, data, commit):
        yaml_content = self.parse_yaml(data['yamlfile'].read().decode('utf-8'))

        for vrf_data in yaml_content['vrfs']:
            self.process_vrf(vrf_data, commit)


class CreateVRF(Script):
    class Meta:
        name = "Create L3VPN (VRF)"
        description = "Create or update a VRF instance based on provided inputs."

    # Define input fields for the script
    vrf_id = IntegerVar(description="VRF ID")
    location = ObjectVar(model=Location, description="Location")
    tenant = ObjectVar(model=Tenant, description="Tenant", required=False, query_params={"name__isw": "svc:"})
    mac_vrfs = MultiObjectVar(model=L2VPN, description="MAC VRFs", query_params={"cf_Service_location": "$location"})
    wan_vrf = ObjectVar(model=VRF, description="WAN VRF", required=False, query_params={"cf_Service_location": "$location"})
    route_target = StringVar(description="Route Target", required=False, regex=re.compile(r'^(?:\d+:\d+)?$'))

    def run(self, data, commit):
        vrf_id = data['vrf_id']
        location = data.get('location')
        tenant = data.get('tenant')
        mac_vrfs = data.get('mac_vrfs')
        wan_vrf = data.get('wan_vrf')
        rt = data.get('route_target')

        defaults = {}

        if tenant:
            defaults['tenant'] = tenant

        # Prepare vrf name
        vrf_name = f"{django_slugify(location.name)}-ipvrf-{vrf_id}"

        # Create or update the VRF instance
        vrf, created = VRF.objects.update_or_create(
            name=vrf_name,
            defaults=defaults
        )
        self.log_success(f"{'Created' if created else 'Updated'} VRF '{vrf.name}'.")

        # Prepare Route Target values
        import_target = rt if rt else f"100:{vrf_id}"
        export_target = rt if rt else f"100:{vrf_id}"

        # Process Route Targets
        import_rt, _ = RouteTarget.objects.get_or_create(name=import_target)
        export_rt, _ = RouteTarget.objects.get_or_create(name=export_target)
        vrf.import_targets.set([import_rt])
        vrf.export_targets.set([export_rt])
        self.log_success(f"Associated import/export Route Targets with L2VPN '{vrf.name}'.")

        vrf.custom_field_data['Vrf_identifier'] = vrf_id

        vrf.custom_field_data['Service_location'] = location.pk

        vrf.custom_field_data['Commissioning_state'] = 'Planned'

        if wan_vrf:
            vrf.custom_field_data['Vrf_wanvrf'] = wan_vrf.pk
            self.log_info(f"Set WAN VRF '{wan_vrf.name}' for VRF '{vrf.name}'.")

        for macvrf in mac_vrfs:
            if macvrf.custom_field_data.get('L2vpn_gateway', None) is None:
                self.log_warning(f"L2VPN '{macvrf.name}' has no gateway set!")
            macvrf.custom_field_data['L2vpn_ipvrf'] = vrf.pk

            if commit:
                macvrf.save()
                self.log_success(f"Associated MAC VRF '{macvrf.name}' with VRF '{vrf.name}'.")

        if commit:
            vrf.save()
            self.log_success("All changes have been committed.")

        return "VRF setup complete."


class DeleteVRF(Script):
    class Meta:
        name = "Delete L3VPN (VRF)"
        description = "Safely delete a selected VRF instance and its associated resources."

    # Allows user to select a VRF instance to delete
    vrf = ObjectVar(
        model=VRF,
        description="Select the VRF instance to delete",
        required=True
    )

    def run(self, data, commit):
        vrf_instance = data['vrf']

        # Begin log message
        self.log_info(f"Initiating deletion process for VRF '{vrf_instance.name}'.")

        # Dissociate any MAC VRFs (L2VPN instances) associated with this VRF
        mac_vrfs_associated = L2VPN.objects.filter(custom_field_data__L2vpn_ipvrf=vrf_instance.pk)
        for macvrf in mac_vrfs_associated:
            # Clear the custom field or set it to None depending on your model's structure
            macvrf.custom_field_data['L2vpn_ipvrf'] = None
            if commit:
                macvrf.save()
                self.log_success(f"Dissociated MAC VRF '{macvrf.name}' from VRF '{vrf_instance.name}'.")

        # Delete associated RouteTargets if necessary
        rt_deletion_candidates = {rt: 0 for rt in itertools.chain(vrf_instance.import_targets.all(), vrf_instance.export_targets.all())}
        l2vpns = L2VPN.objects.prefetch_related('import_targets', 'export_targets').all()
        vrfs = VRF.objects.prefetch_related('import_targets', 'export_targets').all()
        for l2vpn in l2vpns:
            for rt in l2vpn.import_targets.all():
                if rt in rt_deletion_candidates:
                    rt_deletion_candidates[rt] += 1
            for rt in l2vpn.export_targets.all():
                if rt in rt_deletion_candidates:
                    rt_deletion_candidates[rt] += 1
        for vrf in vrfs:
            if vrf == vrf_instance:
                continue
            for rt in vrf.import_targets.all():
                if rt in rt_deletion_candidates:
                    rt_deletion_candidates[rt] += 1
            for rt in vrf.export_targets.all():
                if rt in rt_deletion_candidates:
                    rt_deletion_candidates[rt] += 1
        for rt in rt_deletion_candidates:
            if rt_deletion_candidates[rt] == 0:
                rt.delete()
                self.log_success(f"Deleted RouteTarget '{rt.name}' associated with VRF '{vrf_instance.name}'.")

        # Finally, delete the VRF instance itself
        if commit:
            vrf_instance_name = vrf_instance.name  # Store name for logging after deletion
            vrf_instance.delete()
            self.log_success(f"Deleted VRF '{vrf_instance_name}'.")

        return "VRF deletion process complete."


class ListVPNs(Script):
    class Meta:
        name = "List All VPNs"
        description = "Lists all L2VPN and L3VPN instances with their associated interfaces and details."

    def run(self, data, commit):
        vpn_data = {'L2VPN': [], 'L3VPN': []}

        # Fetch and process all L2VPN instances
        l2vpns = L2VPN.objects.all()
        for l2vpn in l2vpns:
            # Assuming that interfaces are tagged with "l2vpn:<L2VPN name>"
            tag_name = f"l2vpn:{l2vpn.name}"
            interfaces = [interface.name for interface in Interface.objects.filter(tags__name=tag_name)]
            vpn_data['L2VPN'].append({
                'name': l2vpn.name,
                'description': l2vpn.description or "No description",
                'interfaces': interfaces,
            })

        # Fetch and process all VRF instances to identify L3VPN
        vrfs = VRF.objects.all()
        for vrf in vrfs:
            # Manually filtering L2VPNs associated with this VRF
            associated_l2vpns = []
            for l2vpn in L2VPN.objects.all():
                l2vpn_ipvrf = l2vpn.custom_field_data.get('L2vpn_ipvrf')
                if str(vrf.pk) == str(l2vpn_ipvrf):
                    associated_l2vpns.append(l2vpn)

            l2vpn_details = []
            for l2vpn in associated_l2vpns:
                tag_name = f"l2vpn:{l2vpn.name}"
                interfaces = [interface.name for interface in Interface.objects.filter(tags__name=tag_name)]
                l2vpn_details.append(f"{l2vpn.name} (Interfaces: {', '.join(interfaces)})")

            vpn_data['L3VPN'].append({
                'name': vrf.name,
                'description': vrf.description or "No description",
                'associated_l2vpns': l2vpn_details,
            })

        # Format the output
        output = "VPN Listing:\n\n"
        for vpn_type, vpns in vpn_data.items():
            output += f"{vpn_type}:\n"
            for vpn in vpns:
                output += f"  - Name: {vpn['name']}\n    Description: {vpn['description']}\n"
                if vpn_type == 'L2VPN':
                    output += f"    Interfaces: {', '.join(vpn['interfaces'])}\n"
                elif vpn_type == 'L3VPN':
                    output += f"    Associated L2VPNs: {', '.join(vpn['associated_l2vpns'])}\n"
            output += "\n"

        return output


class SetCommissioningState(Script):
    class Meta:
        name = "Set commissioning state"
        description = "Updates the commissioning state on all objects linked to the service specified by the tenant."

    # Define input fields for the script
    tenant = ObjectVar(model=Tenant, description="Tenant", query_params={"name__isw": "svc:"})
    commissioning_state = ChoiceVar(choices=commissioning_state_choices)

    def run(self, data, commit):
        tenant = data['tenant']
        state = data['commissioning_state']

        self.log_info(f"Setting commissioning state to {state} for all services of '{tenant.name}'.")
        # Fetch and process all L2VPN instances
        l2vpns = L2VPN.objects.all()
        for l2vpn in l2vpns:
            if l2vpn.tenant == tenant:
                l2vpn.custom_field_data['Commissioning_state'] = state
                self.log_success(f"Setting commissioning_state on L2VPN '{l2vpn.name}'.")
                if commit:
                    l2vpn.save()

        # Fetch and process all VRF instances to identify L3VPN
        vrfs = VRF.objects.all()
        for vrf in vrfs:
            if vrf.tenant == tenant:
                vrf.custom_field_data['Commissioning_state'] = state
                self.log_success(f"Setting commissioning_state on VRF '{vrf.name}'.")
                if commit:
                    vrf.save()

        return f"Service '{tenant.name}' commissioning state set to {state}"


script_order = (L2VPNsBulkImport, CreateL2VPN, DeleteL2VPN, VRFsBulkImport, CreateVRF,  DeleteVRF, ListVPNs)
