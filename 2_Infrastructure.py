#!/opt/netbox/venv/bin/python
if __name__ == "__main__":
    import os
    import sys
    import django
    
    sys.path.append('/opt/netbox/netbox')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
    django.setup()

import sys
is_migrating = 'migrate' in sys.argv
if not is_migrating:

    import yaml
    import re
    import random
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
        TextVar,
    )
    from extras.models import (
        CustomFieldChoiceSet,
        Tag,
    )
    # from django.utils.text import slugify as django_slugify
    from ipam.models import (
        ASN,
        ASNRange,
        IPAddress,
        Prefix,
        RIR,
        Role,
    )
    from dcim.models import (
        Cable,
        Device,
        DeviceType,
        DeviceRole,
        Interface,
        Location,
        Platform,
        Site,
    )
    # from tenancy.models import Tenant
    # from netaddr import IPAddress as NIPAddress
    from netaddr import IPNetwork
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


    MH_mode_choices = []
    with suppress(CustomFieldChoiceSet.DoesNotExist):
        MH_mode_choices = CustomFieldChoiceSet.objects.get(name="MH_mode").choices


    class ImportFabricFromYAML(Script):
        class Meta:
            name = "Create fabric from YAML"
            description = "Sets up sites, locations, RIRs, ASNs, devices, and management IPs from YAML file."
            field_order = ['yamlfile']

        yamlfile = FileVar(
            description="Upload YAML file for the setup",
        )

        def translate_interface_name(self, short_name):
            """
            Translates short interface names (e1-3) to full names (ethernet-1/3), where
            the short name strictly follows the pattern 'e' followed by a number, a dash, and another number.
            If the name already follows the full format or doesn't match the expected short name pattern,
            it is returned unchanged.
            """
            # Regular expression to match 'e' followed by a number, a dash, and another number
            short_name_pattern = re.compile(r'^e\d+-\d+$')

            # Check if the name is already in the "ethernet-x/y" format
            if re.match(r'ethernet-\d+/\d+', short_name):
                return short_name

            # Translate short names that match the specific pattern
            if short_name_pattern.match(short_name):
                parts = short_name[1:].split("-")
                return f"ethernet-{parts[0]}/{parts[1]}"

            # Return the name unchanged if it doesn't match any expected pattern
            return short_name

        def run(self, data, commit):
            # Read and decode the YAML file content
            uploaded_file = data['yamlfile']
            yaml_content = uploaded_file.read().decode('utf-8')
            yaml_data = yaml.safe_load(yaml_content)

            # Process the site
            site_name = yaml_data['site']['name']
            site, _ = Site.objects.get_or_create(name=site_name, defaults={'slug': slugify(Site, site_name)})
            self.log_success(f"Processed site: {site.name}")

            # Process the location
            location_name = yaml_data['location']['name']
            location, _ = Location.objects.get_or_create(
                name=location_name,
                defaults={
                    'slug': slugify(Location, location_name),
                    'site': site,
                }
            )
            self.log_success(f"Processed location: {location.name}")

            # Ensure a default RIR exists or is created
            default_rir_name = "Private"  # Or any other name you prefer
            default_rir, _ = RIR.objects.get_or_create(
                name=default_rir_name,
                defaults={
                    'slug': slugify(RIR, default_rir_name),
                    'is_private': True  # Assuming the RIR is private, adjust as necessary
                }
            )
            self.log_success(f"Ensured RIR exists: {default_rir.name}")

            # Create the overlay ASN
            overlay_asn_number = yaml_data.get('overlay_asn', {}).get('number')
            if overlay_asn_number:
                overlay_asn, _ = ASN.objects.get_or_create(asn=overlay_asn_number, rir=default_rir)
                location.custom_field_data['Overlay_ASN'] = overlay_asn.id
                location.save()
                location.refresh_from_db()
                self.log_success(f"Assigned Overlay ASN {overlay_asn_number} to location: {location.name}")
            else:
                self.log_warning("Overlay ASN number is missing in the YAML file. Skipped setting Overlay ASN for the location.")

            # Process devices
            for device_info in yaml_data['devices']:
                role = DeviceRole.objects.get(name=device_info['role_name'])
                # Use slug to fetch DeviceType
                device_type = DeviceType.objects.get(slug=device_info['type_slug'])
                platform = Platform.objects.get(slug=device_info['platform_slug'])
                asn_number = device_info['asn_number']
                asn, _ = ASN.objects.get_or_create(asn=asn_number, rir=default_rir)

                device, created = Device.objects.update_or_create(
                    name=device_info['name'],
                    defaults={
                        'device_type': device_type,
                        'device_role': role,
                        'platform': platform,
                        'site': site,
                        'location': location,
                    }
                )

                if created:
                    self.log_success(f"Created device: {device.name}")
                else:
                    self.log_info(f"Device {device.name} already exists.")

                # Manage the management IP
                mgmt_ip, _ = IPAddress.objects.get_or_create(address=device_info['management_ip'])

                # Create or get the management interface 'mgmt0'
                mgmt_interface, _ = Interface.objects.get_or_create(
                    device=device,
                    name='mgmt0',
                    defaults={'type': '1000base-t'}  # Adjust type as needed
                )

                mgmt_interface.ip_addresses.add(mgmt_ip)
                device.primary_ip4 = mgmt_ip
                device.save()
                self.log_success(f"Assigned management IP {mgmt_ip.address} to {device.name}")

                # Update device with ASN custom field
                device.custom_field_data['ASN'] = asn.id
                device.save()
                self.log_success(f"Set ASN {asn_number} for device {device.name}")

                # Process interfaces for the device
                for interface_info in device_info.get('interfaces', []):
                    interface_defaults = {}

                    # Set interface type if provided
                    if 'type' in interface_info and interface_info['type']:
                        interface_defaults['type'] = interface_info['type']

                    interface, created = Interface.objects.get_or_create(
                        device=device,
                        name=interface_info['name'],
                        defaults=interface_defaults
                    )

                    # Assign IP to interface
                    if 'ip_address' in interface_info:
                        ip_address, ip_created = IPAddress.objects.get_or_create(address=interface_info['ip_address'])
                        interface.ip_addresses.add(ip_address)
                        if ip_created:
                            self.log_success(f"Assigned IP {ip_address.address} to interface {interface.name} on device {device.name}")
                        else:
                            self.log_info(f"Interface {interface.name} on device {device.name} already had IP {ip_address.address}")

                    if created:
                        self.log_success(f"Created interface {interface.name} on device {device.name}")
                    else:
                        self.log_info(f"Interface {interface.name} on device {device.name} already exists.")

                for lag_info in device_info.get('lags', []):
                    # Create or get the LAG interface
                    lag_interface, lag_created = Interface.objects.get_or_create(
                        device=device,
                        name=lag_info['name'],
                        defaults={'type': 'lag'}
                    )

                    # Handle Multihome custom fields for the LAG, if present
                    if 'mh_id' in lag_info or 'mh_mode' in lag_info:
                        if 'mh_id' in lag_info:
                            lag_interface.custom_field_data['Iface_mh_id'] = lag_info['mh_id']
                        if 'mh_mode' in lag_info:
                            lag_interface.custom_field_data['Iface_mh_mode'] = lag_info['mh_mode']
                        lag_interface.save()

                    # Process member interfaces for this LAG
                    for member_info in lag_info.get('inteterfaces', []):
                        member_interface, member_created = Interface.objects.get_or_create(
                            device=device,
                            name=member_info['name'],
                        )

                        member_interface.lag = lag_interface
                        member_interface.save()

                        # Log success/info
                        if member_created:
                            self.log_success(f"Created and associated member interface {member_interface.name} with LAG {lag_interface.name}")
                        else:
                            self.log_info(f"Associated existing member interface {member_interface.name} with LAG {lag_interface.name}")

                    if lag_created:
                        self.log_success(f"Created LAG {lag_interface.name} on device {device.name}")
                    else:
                        self.log_info(f"LAG {lag_interface.name} on device {device.name} already exists.")

            # Before processing links, ensure the "isl" tag exists
            isl_tag, created = Tag.objects.get_or_create(name="isl", defaults={'slug': slugify(Tag, "isl")})
            if created:
                self.log_success("Created 'isl' tag.")
            else:
                self.log_info("'isl' tag already exists.")

            # Process interface links from the YAML
            if 'links' in yaml_data:
                for link in yaml_data['links']:
                    device_a_name, interface_a_short = link['endpoints'][0].split(":")
                    device_b_name, interface_b_short = link['endpoints'][1].split(":")

                    self.log_info("Device A: {}, Interface A: {}".format(device_a_name, interface_a_short))
                    self.log_info("Device B: {}, Interface B: {}".format(device_b_name, interface_b_short))

                    interface_a_name = self.translate_interface_name(interface_a_short)
                    interface_b_name = self.translate_interface_name(interface_b_short)

                    # Fetch devices and interfaces from NetBox
                    device_a = Device.objects.get(name=device_a_name)
                    interface_a = Interface.objects.get(device=device_a, name=interface_a_name)

                    device_b = Device.objects.get(name=device_b_name)
                    interface_b = Interface.objects.get(device=device_b, name=interface_b_name)

                    # Check if either interface already has a cable
                    if interface_a.cable or interface_b.cable:
                        self.log_info(f"One of the interfaces already has a cable: {device_a_name}:{interface_a_name} or {device_b_name}:{interface_b_name}")
                    else:
                        # Correct approach to create the cable between the two interfaces
                        cable = Cable(a_terminations=[interface_a], b_terminations=[interface_b], status="connected")
                        if commit:
                            cable.save()
                            self.log_success(f"Cable created between {device_a_name}:{interface_a_name} and {device_b_name}:{interface_b_name}")

                            interface_a.refresh_from_db()
                            interface_b.refresh_from_db()

                            # Add the "isl" tag to both interfaces and save
                            interface_a.tags.add(isl_tag)
                            interface_a.save()
                            interface_b.tags.add(isl_tag)
                            interface_b.save()
                            self.log_success(f"Added 'isl' tag to interfaces: {device_a_name}:{interface_a_name} and {device_b_name}:{interface_b_name}")
                        else:
                            self.log_info(f"Cable would be created between {device_a_name}:{interface_a_name} and {device_b_name}:{interface_b_name} upon commit")

            # Remember to close the uploaded file
            uploaded_file.close()


    class BulkImportLAGsFromYAML(Script):
        class Meta:
            name = "Bulk Import LAGs"
            description = "Imports LAG configurations and their member interfaces from a YAML file."
            field_order = ['yamlfile']

        yamlfile = FileVar(
            description="Upload YAML file containing LAG configurations",
        )

        def run(self, data, commit):
            # Read and decode the YAML file content
            uploaded_file = data['yamlfile']
            yaml_content = uploaded_file.read().decode('utf-8')
            lags_data = yaml.safe_load(yaml_content)

            # Process each LAG configuration
            for lag_info in lags_data.get('lags', []):
                self.process_lag(lag_info, commit)

            uploaded_file.close()

        def process_lag(self, lag_info, commit):
            for device_info in lag_info['devices']:
                device = Device.objects.get(name=device_info['name'])

                # Create or update the LAG interface for the device
                lag_interface, lag_created = Interface.objects.update_or_create(
                    device=device,
                    name=lag_info['name'],
                    defaults={'type': 'lag', 'description': f"LAG Interface for {device_info['name']}"}
                )

                # Apply Multihome custom fields to the LAG
                lag_interface.custom_field_data['Iface_mh_id'] = int(lag_info['mh_id'])
                lag_interface.custom_field_data['Iface_mh_mode'] = lag_info['mh_mode']
                lag_interface.save()

                self.log_success(f"Processed LAG '{lag_interface.name}' for device '{device.name}'")

                # Associate member interfaces with this LAG
                for interface_info in device_info['interfaces']:
                    self.associate_member_with_lag(device, lag_interface, interface_info['name'], commit)

        def associate_member_with_lag(self, device, lag_interface, interface_name, commit):
            # Create or update the member interface and associate it with the LAG
            try:
                member_interface = Interface.objects.get(
                    device=device,
                    name=interface_name,
                )
                member_interface.lag = lag_interface
                if commit:
                    member_interface.save()
                self.log_success(f"Updated member interface '{member_interface.name}' and associated it with LAG '{lag_interface.name}' on device '{device.name}'")
            except Interface.DoesNotExist:
                self.log_warning(f"Member interface '{interface_name}' does not exist on device '{device.name}'")


    class CreateLag(Script):
        class Meta:
            name = "Create a MH Lag"
            description = "Create or update multihome Lag in a guided way"

        # Form fields
        lag_id = IntegerVar(description="Lag ID", min_value=1)
        mh_mode = ChoiceVar(choices=MH_mode_choices, description="Multihome Mode")
        description = StringVar(description="Description", required=False)
        location = ObjectVar(model=Location, description="Location")
        device = ObjectVar(model=Device, description="Device", required=False, query_params={"location": "$location"})
        interfaces = MultiObjectVar(model=Interface, description="Member Interfaces", query_params={"device_id": "$device"})

        def run(self, data, commit):
            lag_id = str(data['lag_id'])  # Ensure lag_id is treated as string for naming consistency
            mh_mode = data['mh_mode']
            description = data.get('description', '')
            selected_interfaces = data['interfaces']

            # Keep track of created or updated LAGs to avoid duplicates on the same device
            processed_devices = set()

            for interface in selected_interfaces:
                device = interface.device

                # Skip if this device's LAG has already been processed
                if device in processed_devices:
                    continue

                lag_name = f"lag{lag_id}"

                # Create or update the LAG interface
                lag_interface, created = Interface.objects.update_or_create(
                    name=lag_name,
                    device=device,
                    defaults={
                        'type': 'lag',
                        'description': description,
                        # Additional LAG interface settings as needed
                    }
                )

                # Set custom fields for the LAG interface
                lag_interface.custom_field_data['Iface_mh_id'] = int(lag_id)
                lag_interface.custom_field_data['Iface_mh_mode'] = mh_mode
                if commit:
                    lag_interface.save()
                    self.log_success(f"{'Created' if created else 'Updated'} LAG '{lag_id}' on device '{device.name}'.")

                processed_devices.add(device)

            # Associate selected interfaces with their respective LAG
            for interface in selected_interfaces:
                # Retrieve or create the LAG interface for this interface's device
                lag_interface = Interface.objects.get(
                    name=f"lag{lag_id}",
                    device=interface.device,
                    type='lag'
                )

                interface.lag = lag_interface
                if commit:
                    interface.save()
                    self.log_success(f"Associated interface '{interface.name}' with LAG '{lag_id}' on device '{interface.device.name}'.")

            if commit:
                self.log_success("All changes have been committed.")
                return "LAG setup complete."
            else:
                return "LAG setup preview complete. No changes have been made."


    class DeleteLag(Script):
        class Meta:
            name = "Delete a MH Lag"
            description = "Safely delete a selected multihome LAG and disassociate its member interfaces."

        @staticmethod
        def generate_lags_description():
            lags = Interface.objects.filter(type='lag').select_related('device', 'device__location')

            # Extract lags into a list of dictionaries for easier sorting
            lags_list = []
            for lag in lags:
                mh_id = int(lag.custom_field_data.get('Iface_mh_id', 0))
                location_name = lag.device.location.name if lag.device.location else 'Unknown Location'
                lags_list.append({
                    'mh_id': mh_id,
                    'name': lag.name,
                    'device': lag.device.name,
                    'location': location_name
                })

            lags_list.sort(key=lambda x: x['mh_id'])

            # Build the description string from the sorted list
            lags_description = ""
            for lag in lags_list:
                lags_description += f"LAG ID: {lag['mh_id']}, Name: {lag['name']}, Device: {lag['device']}, Location: {lag['location']}\n"

            return lags_description if lags_description else "No LAG interfaces found."

        available_lags = generate_lags_description()

        lags_info = TextVar(
            description="Available LAGs",
            required=False,
            default=available_lags
        )

        location = ObjectVar(
            model=Location,
            description="Location",
            required=True,
        )

        lag_mh_id = IntegerVar(
            description="Enter the LAG Multihome ID (refer to the LAGs information above)",
            required=True
        )

        def run(self, data, commit):
            location = data['location']
            mh_id = int(data['lag_mh_id'])

            # Find all LAG interfaces within the specified location that match the mh_id.
            lags_to_delete = Interface.objects.filter(
                custom_field_data__Iface_mh_id=mh_id,
                device__location=location,
                type='lag'
            )

            if not lags_to_delete.exists():
                return f"No LAG interfaces found with MH ID '{mh_id}' in the specified location."

            deleted_lags_info = []  # Collect info about deleted LAGs for logging

            for lag_interface in lags_to_delete:
                # Collect member interfaces for this LAG
                member_interfaces = Interface.objects.filter(lag=lag_interface)

                # Disassociate any member interfaces from this LAG
                for interface in member_interfaces:
                    interface.lag = None
                    if commit:
                        interface.save()
                        self.log_success(f"Disassociated member interface '{interface.name}' from LAG '{lag_interface.name}'.")

                # Delete the LAG interface
                if commit:
                    deleted_lags_info.append(f"'{lag_interface.name}' on device '{lag_interface.device.name}'")
                    lag_interface.delete()

            if commit:
                deleted_lags_str = ", ".join(deleted_lags_info)
                self.log_success(f"Deleted LAGs: {deleted_lags_str}.")
                return f"Successfully deleted LAGs: {deleted_lags_str} and disassociated all member interfaces."
            else:
                # In a no-commit scenario, just log what would have happened
                preview_info = ", ".join([f"'{lag.name}' on device '{lag.device.name}'" for lag in lags_to_delete])
                return f"Would delete LAGs: {preview_info} and disassociate all member interfaces. No changes made due to dry run."


    class CreateFabric(Script):
        class Meta:
            name = "CreateFabric"
            description = "Automated Fabric Creation"

        @staticmethod
        def get_default_device_type(model_name):
            return DeviceType.objects.filter(model=model_name).first()

        spine_model_default = DeviceType.objects.get(slug='nokia-7220-ixr-d2l-25-100ge')
        leaf_model_default = DeviceType.objects.get(slug='nokia-7220-ixr-d3l-32-100ge')

        site_name = StringVar(description="Name of the site", default="Antwerp")
        location_name = StringVar(description="Name of the location", default="DC3")
        num_dcgws = IntegerVar(description="Number of dcgws", default=2)
        num_spines = IntegerVar(description="Number of spines", default=2)
        spine_model = ObjectVar(model=DeviceType, description="Spine Model", default=spine_model_default)
        num_leaves = IntegerVar(description="Number of leaves", default=3)
        leaf_model = ObjectVar(model=DeviceType, description="Leaf Model", default=leaf_model_default)
        management_ip_subnet = IPAddressWithMaskVar(
            description="Management IP Subnet",
            default="192.168.1.0/24"
        )
        system_ip_subnet = IPAddressWithMaskVar(
            description="System IP Subnet",
            default="10.0.0.0/24"
        )
        isl_network_subnet = IPAddressWithMaskVar(
            description="Subnet for the ISL Links",
            default="172.16.0.0/24"
        )
        asn_range = StringVar(
            description="Range of ASNs",
            default="65001-65100"
        )

        def get_free_asn(self, asn_range):
            """Retrieve a free ASN within the specified ASNRange object."""
            used_asns = ASN.objects.filter(asn__range=(asn_range.start, asn_range.end)).values_list('asn', flat=True)
            for asn in range(asn_range.start, asn_range.end + 1):
                if asn not in used_asns:
                    return asn
            raise ValueError("No free ASN available within the specified range.")

        def assign_ip_address(self, device, interface_name, prefix):
            """
            Assign an IP address from the specified prefix to the specified interface of a device,
            based on the prefix role. For management IPs, also set the IP as the primary IP for the device.
            """
            # Generate a list of available IP addresses within the prefix
            available_ips_list = list(prefix.get_available_ips())

            if not available_ips_list:
                self.log_failure(f"No available IP addresses in prefix {prefix} for {interface_name} on {device.name}.")
                return None

            # Determine the subnet mask
            subnet_mask = '/32' if prefix.role.slug == 'system' else f"/{prefix.prefix.prefixlen}"
            ip_address_str = f"{available_ips_list[0]}{subnet_mask}"

            ip_obj, created = IPAddress.objects.get_or_create(
                address=ip_address_str,
                defaults={
                    'status': 'active',
                    'description': f"{prefix.role.name} IP for {device.name}",
                }
            )

            # Retrieve or create the specified interface for the device
            interface, interface_created = Interface.objects.get_or_create(
                device=device,
                name=interface_name,
                defaults={'type': 'virtual' if prefix.role.slug == 'system' else '1000base-t'}
            )

            # Associate the IP with the interface and save
            if prefix.role.slug != 'system':
                # For non-system IPs, directly assign and save
                ip_obj.assigned_object = interface
                ip_obj.save()
            else:
                # For system IPs, add to the interface's IP list but not set as primary IP of the device
                interface.ip_addresses.add(ip_obj)

            action = "Assigned" if created else "Reassigned"
            self.log_success(f"{action} {ip_obj.address} to {interface_name} on {device.name}.")

            # Specifically handle the management IP: assign to interface and set as primary
            if prefix.role.slug == 'management':
                device.primary_ip4 = ip_obj
                device.save()
                self.log_success(f"Set {ip_obj.address} as primary management IP for {device.name}.")

        def create_isl_links(self, leaves, spines, dcgws, isl_prefix):
            # Helper function to get the last available Ethernet interfaces on a device
            def get_last_available_interfaces(device, count):
                eligible_interfaces = list(Interface.objects.filter(device=device, tagged_vlans=None, lag=None, cable=None).exclude(name__contains='.').exclude(type='virtual').exclude(name__contains='mgmt').exclude(type="lag").exclude(type='10gbase-x-sfpp').order_by('-_name'))

                return eligible_interfaces[:count]

            # Helper function to get the first available Ethernet interfaces on a device with an offset
            #                                     spine, 1, 2
            def get_first_available_interfaces(device, count, offset):
                eligible_interfaces = list(Interface.objects.filter(device=device, tagged_vlans=None, lag=None, cable=None).exclude(name__contains='.').exclude(type='virtual').exclude(name__contains='mgmt').exclude(type="lag").exclude(type='10gbase-x-sfpp').order_by('_name'))

                return eligible_interfaces[offset:offset+count]

            def assign_isl_ip_addresses(interface_a, interface_b, isl_prefix):
                # Convert the larger prefix into an IPNetwork object
                isl_network = IPNetwork(isl_prefix.prefix)

                # Iterate over all possible /31 subnets within the larger network
                for subnet in isl_network.subnet(31):
                    # Convert the subnet into a list of IPs and ensure there are exactly 2 (as expected in a /31 subnet)
                    ips = list(subnet)
                    if len(ips) != 2:
                        continue

                    ip_a, ip_b = str(ips[0]) + '/31', str(ips[1]) + '/31'

                    # Check if both IP addresses are available
                    if not IPAddress.objects.filter(address__in=[ip_a, ip_b]).exists():

                        # Create or update the IPAddress objects
                        ip_obj_a, created_a = IPAddress.objects.get_or_create(
                            address=ip_a,
                            defaults={
                                'status': 'active',
                                'description': f"ISL IP for {interface_a.device.name}",
                            }
                        )
                        ip_obj_b, created_b = IPAddress.objects.get_or_create(
                            address=ip_b,
                            defaults={
                                'status': 'active',
                                'description': f"ISL IP for {interface_b.device.name}",
                            }
                        )

                        # Assign the IPAddress objects to the interfaces
                        interface_a.ip_addresses.add(ip_obj_a)
                        interface_b.ip_addresses.add(ip_obj_b)

                        # Log the assignment and exit the function as the IPs have been assigned
                        self.log_success(f"Assigned IP addresses {ip_a} and {ip_b} to interfaces {interface_a.name} and {interface_b.name}.")
                        return

                # If this point is reached, no suitable /31 subnets were found
                self.log_failure(f"No available /31 subnets found in ISL prefix {isl_prefix}.")

            # Helper function to connect two interfaces
            def connect_interfaces(interface_a, interface_b, isl_prefix):
                c = Cable(a_terminations=[interface_a], b_terminations=[interface_b], status="connected")

                interface_a.refresh_from_db()
                interface_b.refresh_from_db()

                isl_tag, created = Tag.objects.get_or_create(name="isl", slug="isl")

                # Add the "isl" tag to both interfaces and save
                interface_a.tags.add(isl_tag)
                interface_a.save()
                interface_b.tags.add(isl_tag)
                interface_b.save()
                c.save()

                assign_isl_ip_addresses(interface_a, interface_b, isl_prefix)

                self.log_success(f"Connected {interface_a.device.name}:{interface_a.name} to {interface_b.device.name}:{interface_b.name}")

            # Connect Leaves to Spines
            for i, leaf in enumerate(leaves, start=1):
                leaf_interfaces = get_last_available_interfaces(leaf, len(spines))
                for j, spine in enumerate(spines, start=1):
                    spine_interfaces = get_first_available_interfaces(spine, 2, 2)
                    spine_interface = get_first_available_interfaces(spine, 1, 2)[0]
                    connect_interfaces(leaf_interfaces[j-1], spine_interface, isl_prefix)

            # Ensure DCGW interfaces are generated
            for dcgw in dcgws:
                self.ensure_dcgw_interfaces(dcgw, len(spines))

            # Connect Spines to DCGWs
            for i, spine in enumerate(spines, start=1):
                # Retrieve the last available interfaces on the spine for each DCGW connection
                spine_interfaces = get_last_available_interfaces(spine, len(dcgws))
                for j, dcgw in enumerate(dcgws, start=1):
                    dcgw_interface = Interface.objects.get(device=dcgw, name=f"1/1/c{i}/1")
                    connect_interfaces(spine_interfaces[j-1], dcgw_interface, isl_prefix)

        def ensure_dcgw_interfaces(self, dcgw, count):
            # Check existing count and create additional interfaces if needed
            existing_interfaces = Interface.objects.filter(
                device=dcgw,
                name__startswith='1/1/c'
            ).count()

            for i in range(existing_interfaces, count):
                Interface.objects.create(
                    device=dcgw,
                    name=f"1/1/c{i+1}/1",
                    type="100gbase-x-cfp4"
                )

        def run(self, data, commit):
            # Basic validations and setup
            site_name = data.get('site_name', 'test')
            location_name = data.get('location_name', 'dc3')
            num_dcgws = data.get('num_dcgws', 0)
            num_leaves = data.get('num_leaves', 3)
            leaf_model = data.get('leaf_model')
            num_spines = data.get('num_spines', 2)
            spine_model = data.get('spine_model')
            management_ip_subnet = data.get('management_ip_subnet')
            system_ip_subnet = data.get('system_ip_subnet')
            isl_network_subnet = data.get('isl_network_subnet')
            asn_range = data.get('asn_range')

            # Create or get the site, location and tenant
            site, _ = Site.objects.get_or_create(name=site_name, defaults={'slug': slugify(Site, site_name)})
            self.log_success(f"Site {site_name} created or retrieved successfully.")

            location, _ = Location.objects.get_or_create(
                name=location_name,
                defaults={
                    'slug': slugify(Location, location_name),
                    'site': site,
                }
            )
            self.log_success(f"Location {location_name} created or retrieved successfully.")

            # tenant, _ = Tenant.objects.get_or_create(name=site_name, slug=slugify(Site, site_name))
            # self.log_success(f"Tenant {site_name} created or retrieved successfully.")

            # Create prefix roles for mgmt, system and isl
            management_prefix_role, _ = Role.objects.get_or_create(name='Management', slug='management')
            system_prefix_role, _ = Role.objects.get_or_create(name='System', slug='system')
            isl_prefix_role, _ = Role.objects.get_or_create(name='ISL', slug='isl')

            # Create prefixes for mgmt, sysstem and isl
            management_prefix, _ = Prefix.objects.get_or_create(prefix=management_ip_subnet, site=site, role=management_prefix_role)
            system_prefix, _ = Prefix.objects.get_or_create(prefix=system_ip_subnet, site=site, role=system_prefix_role)
            isl_prefix, _ = Prefix.objects.get_or_create(prefix=isl_network_subnet, site=site, role=isl_prefix_role)
            self.log_success("IP Subnets for Management, System, and ISL created or retrieved successfully.")

            # Create ASNs from user range
            asn_start, asn_end = [int(asn) for asn in asn_range.split('-')]

            # Ensure the RIR for ASNRange creation
            rir, _ = RIR.objects.get_or_create(name='Private', slug='private')

            # Adjusted to create an ASNRange with required fields
            asn_range_obj, created = ASNRange.objects.get_or_create(
                name=f"{site_name}_asn_range",
                slug=slugify(ASNRange, f"{site_name}_asn_range"),
                start=asn_start,
                end=asn_end,
                rir=rir,
                # tenant=tenant,
            )
            if created:
                self.log_success(f"ASN range {asn_range_obj.range_as_string()} created successfully under RIR {rir.name}.")
            else:
                self.log_info(f"Using existing ASN range {asn_range_obj.range_as_string()}.")

            # Device roles
            spine_role, _ = DeviceRole.objects.get_or_create(name="spine", slug="spine")
            leaf_role, _ = DeviceRole.objects.get_or_create(name="leaf", slug="leaf")
            dcgw_role, _ = DeviceRole.objects.get_or_create(name="dcgw", slug="dcgw")
            self.log_success("Device roles for Spine, Leaf, and DCGW created or retrieved successfully.")

            # Create devices (spines) with the same ASN (all spines get the same ASN) (ASN is a custom field at device )
            spine_asn_value = self.get_free_asn(asn_range_obj)
            spine_asn, _ = ASN.objects.get_or_create(asn=spine_asn_value, rir=rir, defaults={'description': f"{site_name} Spines"})
            spine_devices = []

            for i in range(1, num_spines + 1):
                spine_name = f"{site.name}-spine-{i}"
                spine, created = Device.objects.get_or_create(
                    name=spine_name,
                    defaults={
                        'device_role': spine_role,
                        'device_type': spine_model,
                        'site': site,
                        'location': location,
                    }
                )
                spine.custom_field_data['ASN'] = spine_asn.id  # Store ASN value
                spine.save()
                self.log_success(f"Spine {spine_name} created with ASN {spine_asn_value}.")
                spine_devices.append(spine)

            # Create devices (leaves) with individual ASNs
                leaf_devices = []
            for i in range(1, num_leaves + 1):
                leaf_asn_value = self.get_free_asn(asn_range_obj)
                leaf_asn, _ = ASN.objects.get_or_create(asn=leaf_asn_value, rir=rir, defaults={'description': f"{site_name} Leaf"})

                leaf_name = f"{site.name}-leaf-{i}"
                leaf, created = Device.objects.get_or_create(
                    name=leaf_name,
                    defaults={
                        'device_role': leaf_role,
                        'device_type': leaf_model,
                        'site': site,
                        'location': location,
                    }
                )
                leaf.custom_field_data['ASN'] = leaf_asn.id
                leaf.save()
                self.log_success(f"Leaf {leaf_name} created with ASN {leaf_asn_value}.")
                leaf_devices.append(leaf)

            # Create devices (dcgws) with individual ASNs
            dcgw_devices = []
            for i in range(1, num_dcgws + 1):
                dcgw_asn_value = self.get_free_asn(asn_range_obj)
                dcgw_asn, _ = ASN.objects.get_or_create(asn=dcgw_asn_value, rir=rir, defaults={'description': f"{site_name} DCGW"})

                try:
                    dcgw_model = DeviceType.objects.get(slug='nokia-7750-sr-1')
                except DeviceType.DoesNotExist:
                    raise AbortScript("Cant't find devicetype with slug nokia-7750-sr-1!")

                dcgw_name = f"{site.name}-dcgw-{i}"
                dcgw, created = Device.objects.get_or_create(
                    name=dcgw_name,
                    defaults={
                        'device_role': dcgw_role,
                        'device_type': dcgw_model,
                        'site': site,
                        'location': location,
                    }
                )
                dcgw.custom_field_data['ASN'] = dcgw_asn.id
                dcgw.save()
                self.log_success(f"DCGW {dcgw_name} created with ASN {dcgw_asn_value}.")
                dcgw_devices.append(dcgw)

            # Create a system0 interfaces (virtual) on all devices
            devices = Device.objects.filter(site=site)
            for device in devices:
                # Create or get the system0 interface for each device
                system0_interface, created = Interface.objects.get_or_create(
                    device=device,
                    name="system0",
                    type="virtual",
                )
                if created:
                    self.log_success(f"Created system0 interface for device {device.name}.")

                self.assign_ip_address(device, "mgmt0", management_prefix)
                self.assign_ip_address(device, "system0", system_prefix)

            # Create ISL links between spines, leaves, and dcgws
            self.create_isl_links(leaf_devices, spine_devices, dcgw_devices, isl_prefix)

            return "Fabric creation process completed."


    # class DeleteFabric(Script):
    #     class Meta:
    #         name = "Delete a Nokia fabric"
    #         description = "Delete a Nokia fabric in a guided way"
    #
    #         field_order = ['yamlfile']
    #
    #     yamlfile = FileVar(
    #         description="Upload YAML file for the setup",
    #     )
    #
    #     # Main method to run the script
    #     def run(self, data, commit):
    #         # Assuming 'data' contains the YAML content
    #         yaml_content = self.parse_yaml(data['yamlfile'].read().decode('utf-8'))
    #
    #         pass


    # script_order = (ImportFabricFromYAML, CreateFabric, DeleteFabric, BulkImportLAGsFromYAML, CreateLag, DeleteLag)
    script_order = (ImportFabricFromYAML, CreateFabric, BulkImportLAGsFromYAML, CreateLag, DeleteLag)
