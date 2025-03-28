#!/opt/netbox/venv/bin/python
if __name__ == "__main__":
    import os
    import sys
    import django
    sys.path.append('/app/netbox/netbox')  # Updated path to the correct location
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
    django.setup()

import sys
is_migrating = 'migrate' in sys.argv
if not is_migrating:

    import re
    import random

    from extras.scripts import Script
    # Update imports for NetBox 4.2
    from extras.models import (
        ConfigContext,
        CustomField,
        CustomFieldChoiceSet,
    )
    from extras.choices import CustomFieldTypeChoices

    from django.core.exceptions import ValidationError
    from django.contrib.contenttypes.models import ContentType

    from ipam.models import (
        ASN,
        IPAddress,
        VRF,
    )

    # In NetBox 4.2, L2VPN is moved to vpn app
    from vpn.models import L2VPN

    from dcim.models import (
        Device,
        DeviceRole,
        DeviceType,
        Interface,
        InterfaceTemplate,
        Location,
        Manufacturer,
        Platform,
    )


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
            raise Exception("It's not your lucky day - unable to create a unique slug")


    class InitializeNetbox(Script):
        class Meta:
            name = "Initialize Netbox"
            description = "This script initializes NetBox by setting up predefined device roles, platforms, configuration contexts, add a Nokia SR1"

        def create_or_update_choice_set(self, name, choices, description=None):
            """
            Create or update a custom field choice set in NetBox 4.2
            """
            # Create the choice set
            choice_set, created = CustomFieldChoiceSet.objects.get_or_create(
                name=name,
                defaults={
                    "description": description or f"Choice set for {name}",
                    "extra_choices": choices
                }
            )

            # If it already exists, update the choices
            if not created:
                choice_set.extra_choices = choices
                choice_set.save()
                self.log_info(f"Updated existing choice set: {name}")
            else:
                self.log_success(f"Created new choice set: {name}")

            return choice_set

        def create_or_update_custom_field(self, name, type, choice_set=None, content_types=None, object_type=None, **kwargs):
            """
            Create or update a custom field in NetBox 4.2
            """
            defaults = {
                'type': type,
            }

            # Incorporate any additional keyword arguments into the defaults
            for key, value in kwargs.items():
                if value is not None:
                    defaults[key] = value

            # Set the choice_set if provided
            if choice_set and type == CustomFieldTypeChoices.TYPE_SELECT:
                defaults['choice_set'] = choice_set

            # Create the custom field without content_type first
            custom_field, created = CustomField.objects.update_or_create(
                name=name,
                defaults=defaults
            )

            if created:
                self.log_success(f"Created new custom field: {name}")
            else:
                self.log_info(f"Custom field '{name}' already exists or updated.")

            # Handle content types - renamed to object_types in NetBox 4.2
            if content_types:
                if not isinstance(content_types, (list, tuple)):
                    content_types = [content_types]

                # Convert ContentType objects to IDs if needed
                content_type_ids = [ct.id if hasattr(ct, 'id') else ct for ct in content_types]

                # Set object_types
                custom_field.object_types.set(content_type_ids)
                self.log_success(f"Custom field '{name}' associated with specified content types.")

            # For TYPE_OBJECT fields, we need to set the content type relationship
            if type == CustomFieldTypeChoices.TYPE_OBJECT and object_type and hasattr(custom_field, 'content_type_id'):
                # Update content_type_id if that field exists
                custom_field.content_type_id = object_type.id
                custom_field.save()
                self.log_success(f"Set content type for '{name}' to: {object_type}")
            elif type == CustomFieldTypeChoices.TYPE_OBJECT and object_type:
                # Try alternative approaches if content_type_id doesn't exist
                try:
                    # Look for the field by inspecting the model fields
                    for field_name in dir(custom_field):
                        if field_name.endswith('_type_id') or field_name.endswith('_content_type_id'):
                            setattr(custom_field, field_name, object_type.id)
                            custom_field.save()
                            self.log_success(f"Set {field_name} for '{name}' to: {object_type}")
                            break
                    else:
                        self.log_warning(f"Could not find field to set object type for '{name}'")

                except Exception as e:
                    self.log_warning(f"Failed to set object type for '{name}': {e}")

            return custom_field

        def create_manufacturer(self):
            manufacturer_name = "Nokia"
            manufacturer, created = Manufacturer.objects.get_or_create(name=manufacturer_name, defaults={"slug": slugify(Manufacturer, manufacturer_name)})
            if created:
                self.log_success(f"Manufacturer '{manufacturer_name}' created.")
            else:
                self.log_info(f"Manufacturer '{manufacturer_name}' already exists.")
            return manufacturer

        def create_device_type(self, manufacturer):
            device_type_name = "7750 SR-1"
            device_type, created = DeviceType.objects.get_or_create(
                model=device_type_name,
                defaults={
                    "slug": slugify(DeviceType, device_type_name),
                    "manufacturer": manufacturer,
                    "u_height": 2,
                    "is_full_depth": True
                }
            )
            if created:
                self.log_success(f"Device type '{device_type_name}' created.")
            else:
                self.log_info(f"Device type '{device_type_name}' already exists.")
            return device_type

        def create_interface_template(self, device_type):
            interface_name = "mgmt0"
            interface_template, created = InterfaceTemplate.objects.get_or_create(
                device_type=device_type,
                name=interface_name,
                defaults={
                    "type": "1000base-t",
                    "mgmt_only": True
                }
            )
            if created:
                self.log_success(f"Management interface '{interface_name}' created for device type '{device_type.model}'.")
            else:
                self.log_info(f"Management interface '{interface_name}' already exists for device type '{device_type.model}'.")

        def run(self, data, commit):

            # Define device roles to be created
            device_roles = [
                {"name": "leaf", "slug": "leaf"},
                {"name": "spine", "slug": "spine"},
                {"name": "dcgw", "slug": "dcgw"},
                {"name": "superspine", "slug": "superspine"},
                {"name": "borderleaf", "slug": "borderleaf"}
            ]

            # Create device roles
            for role in device_roles:
                DeviceRole.objects.get_or_create(**role)
                self.log_success(f"Device role '{role['name']}' ensured.")

            # Define platforms to be created
            platform_dict = {}  # Store platform objects for later reference
            platforms_data = [
                {"name": "SRL", "slug": "srl"},
                {"name": "SROS", "slug": "sros"}
            ]

            # Create platforms
            for platform_data in platforms_data:
                platform, created = Platform.objects.get_or_create(**platform_data)
                platform_dict[platform_data['slug']] = platform  # Map slug to platform object
                action = "Created" if created else "Found"
                self.log_success(f"{action} platform '{platform_data['name']}'.")

            # Define config contexts to be created
            config_contexts = [
                {
                    "name": "Ansible_SRLinux",
                    "data": {
                        "ansible_user": "admin",
                        "ansible_become": "no",
                        "ansible_password": "NokiaSrl1!",
                        "ansible_connection": "httpapi",
                        "ansible_network_os": "nokia.srlinux.srlinux",
                        "ansible_command_timeout": 900,
                        "ansible_httpapi_ciphers": "ECDHE-RSA-AES256-SHA",
                        "ansible_httpapi_use_ssl": True,
                        "ansible_httpapi_validate_certs": False
                    },
                    "platforms": ["srl"]
                },
                {
                    "name": "platform_srlinux",
                    "data": {"platform": "srlinux"},
                    "is_active": True,
                    "platforms": ["srl"]
                },
                {
                    "name": "Ansible_SROS",
                    "data": {
                        "ansible_ssh_pass": "admin",
                        "ansible_ssh_user": "admin",
                        "ansible_connection": "ansible.netcommon.netconf",
                        "ansible_host_key_checking": False
                    },
                    "platforms": ["sros"]
                },
                {
                    "name": "platform_sros",
                    "data": {"platform": "sros"},
                    "is_active": True,
                    "platforms": ["sros"]
                },
            ]

            for context in config_contexts:
                platforms = [platform_dict[slug] for slug in context["platforms"]]
                ctx, created = ConfigContext.objects.get_or_create(name=context["name"], defaults={"data": context["data"], "is_active": True})

                if created:
                    self.log_success(f"Config context '{context['name']}' created.")
                else:
                    self.log_info(f"Config context '{context['name']}' already exists.")

                for platform in platforms:
                    ctx.platforms.add(platform)  # Add each platform to the config context
                ctx.save()
                self.log_success(f"Platform(s) assigned to config context '{context['name']}'.")

            nokia_manufacturer = self.create_manufacturer()
            sr1_device_type = self.create_device_type(nokia_manufacturer)
            self.create_interface_template(sr1_device_type)

            # Define choice sets with choices for NetBox 4.2
            choice_sets_info = {
                "Service_commissioning_state": [
                    ["Planned", "Planned"],
                    ["Commissioned", "Commissioned"],
                    ["Deleted", "Deleted"]
                ],
                "Service_deployment_state": [
                    ["Success", "Success"],
                    ["Failed", "Failed"]
                ],
                "MH_mode": [
                    ["all-active", "All active"],
                    ["single-active", "Single active"]
                ]
            }

            # First create the choice sets
            choice_sets = {}
            for name, choices in choice_sets_info.items():
                choice_sets[name] = self.create_or_update_choice_set(
                    name=name,
                    choices=choices,
                    description=f"Choice set for {name.replace('_', ' ')}"
                )

            content_types_ipam = [
                ContentType.objects.get_for_model(VRF),
                ContentType.objects.get_for_model(L2VPN),
            ]

            # Create the custom fields with choice sets (NetBox 4.2 way)
            # Create the 'Commissioning_state' custom field
            self.create_or_update_custom_field(
                name='Commissioning_state',
                type=CustomFieldTypeChoices.TYPE_SELECT,
                description='The commissioning state of the service.',
                choice_set=choice_sets["Service_commissioning_state"],
                content_types=content_types_ipam
            )

            # Create the 'Deployment_state' custom field
            self.create_or_update_custom_field(
                name='Deployment_state',
                type=CustomFieldTypeChoices.TYPE_SELECT,
                description='The deployment state of the service.',
                choice_set=choice_sets["Service_deployment_state"],
                content_types=content_types_ipam
            )

            # Create the 'Iface_mh_mode' custom field
            self.create_or_update_custom_field(
                name='Iface_mh_mode',
                type=CustomFieldTypeChoices.TYPE_SELECT,
                description='Multi Home mode',
                label='Mode',
                group_name="Multi-homing access",
                choice_set=choice_sets["MH_mode"],
                content_types=ContentType.objects.get_for_model(Interface)
            )

            # Create the 'Iface_mh_id' custom field
            self.create_or_update_custom_field(
                name='Iface_mh_id',
                type=CustomFieldTypeChoices.TYPE_INTEGER,
                description='Multi Home mode',
                label='ID',
                group_name="Multi-homing access",
                content_types=ContentType.objects.get_for_model(Interface)
            )

            # Create the 'Service_location' custom field
            self.create_or_update_custom_field(
                name='Service_location',
                type=CustomFieldTypeChoices.TYPE_OBJECT,
                label='Location',
                description='Service location.',
                content_types=content_types_ipam,
                object_type=ContentType.objects.get_for_model(Location)
            )

            # Create the 'Vrf_wanvrf' custom field
            self.create_or_update_custom_field(
                name='Vrf_wanvrf',
                type=CustomFieldTypeChoices.TYPE_OBJECT,
                label='WAN-VRF',
                description='Associates a VRF to WAN VRF.',
                content_types=[ContentType.objects.get_for_model(VRF)],
                object_type=ContentType.objects.get_for_model(VRF)
            )

            # VRF Identifier custom field
            self.create_or_update_custom_field(
                name='Vrf_identifier',
                type=CustomFieldTypeChoices.TYPE_INTEGER,
                label='Identifier',
                description='Identifier for VRF.',
                content_types=[ContentType.objects.get_for_model(VRF)]
            )

            # L2VPN VLAN custom field
            self.create_or_update_custom_field(
                name='L2vpn_vlan',
                type=CustomFieldTypeChoices.TYPE_TEXT,
                label='802.1Q',
                description='VLAN for L2VPN.',
                content_types=[ContentType.objects.get_for_model(L2VPN)],
                validation_regex=r"^(?:untagged|409[0-5]|40[0-8][0-9]|[0-3]?[0-9]{1,3})$"
            )

            # L2VPN Gateway custom field
            self.create_or_update_custom_field(
                name='L2vpn_gateway',
                type=CustomFieldTypeChoices.TYPE_OBJECT,
                label='Gateway',
                description='Gateway IP address for L2VPN.',
                group_name="L2VPN VRF association",
                content_types=[ContentType.objects.get_for_model(L2VPN)],
                object_type=ContentType.objects.get_for_model(IPAddress)
            )

            # L2VPN IP VRF custom field
            self.create_or_update_custom_field(
                name='L2vpn_ipvrf',
                type=CustomFieldTypeChoices.TYPE_OBJECT,
                label='IP-VRF',
                description='IP VRF for L2VPN.',
                group_name="L2VPN VRF association",
                content_types=[ContentType.objects.get_for_model(L2VPN)],
                object_type=ContentType.objects.get_for_model(VRF)
            )

            # Device ASN custom field
            self.create_or_update_custom_field(
                name='ASN',
                type=CustomFieldTypeChoices.TYPE_OBJECT,
                label=None,
                description='Autonomous System Number for devices.',
                content_types=[ContentType.objects.get_for_model(Device)],
                object_type=ContentType.objects.get_for_model(ASN)
            )

            # Site Overlay ASN custom field
            self.create_or_update_custom_field(
                name='Overlay_ASN',
                type=CustomFieldTypeChoices.TYPE_OBJECT,
                label=None,
                description='Overlay ASN for locations.',
                content_types=[ContentType.objects.get_for_model(Location)],
                object_type=ContentType.objects.get_for_model(ASN)
            )