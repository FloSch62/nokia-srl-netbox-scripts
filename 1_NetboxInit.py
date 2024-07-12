#!/opt/netbox/venv/bin/python
if __name__ == "__main__":
    import os
    import sys
    import django
    sys.path.append('/opt/netbox/netbox')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
    django.setup()

import re
import random

from extras.scripts import Script, AbortScript
from extras.models import (
    ConfigContext,
    CustomField,
    CustomFieldChoiceSet,
)
from django.core.exceptions import ValidationError
from django.contrib.contenttypes.models import ContentType
# from django.utils.text import slugify as django_slugify
from ipam.models import (
    ASN,
    IPAddress,
    VRF,
)
try:
    from ipam.models import L2VPN
except ImportError:
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
        raise AbortScript("It's not your lucky day - unable to create a unique slug")


class InitializeNetbox(Script):
    class Meta:
        name = "Initialize Netbox"
        description = "This script initializes NetBox by setting up predefined device roles, platforms, configuration contexts, add a Nokia SR1"

    def create_or_update_choice_set(self, name, choices, **kwargs):

        defaults = {
            'order_alphabetically': False
        }

        for key, value in kwargs.items():
            if value is not None:
                defaults[key] = value

        choice_set, created = CustomFieldChoiceSet.objects.get_or_create(
            name=name,
            defaults=defaults
        )
        choice_set.extra_choices = choices
        try:
            choice_set.clean()
            choice_set.save()
            if created:
                self.log_success(f"Created choice set: {name}")
            else:
                self.log_info(f"Updated choice set: {name}")
        except ValidationError as e:
            self.log_failure(f"Validation failed for '{name}': {str(e)}")

    def create_or_update_custom_field(self, name, type, choice_set=None, content_types=None, object_type=None, **kwargs):
        defaults = {
            'type': type,
        }
        if choice_set:
            defaults['choice_set'] = choice_set
        if object_type:  # For object type fields
            defaults['object_type'] = object_type

        # Incorporate any additional keyword arguments into the defaults
        for key, value in kwargs.items():
            if value is not None:
                defaults[key] = value

        custom_field, created = CustomField.objects.update_or_create(
            name=name,
            defaults=defaults
        )

        if created:
            self.log_success(f"Created new custom field: {name}")
        else:
            self.log_info(f"Custom field '{name}' already exists or updated.")

        # Ensure content_types is always an iterable
        if not isinstance(content_types, (list, tuple)):
            content_types = [content_types]

        # Update content_types for the custom field
        custom_field.content_types.set(content_types)
        self.log_success(f"Custom field '{name}' associated with specified content types.")

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

        for name, choices in choice_sets_info.items():
            self.create_or_update_choice_set(name, choices)

        content_types_ipam = [
            ContentType.objects.get_for_model(VRF),
            ContentType.objects.get_for_model(L2VPN),
        ]

        # Create the 'Commissioning_state' custom field
        self.create_or_update_custom_field(
            name='Commissioning_state',
            type='select',
            description='The commissioning state of the service.',
            choice_set=CustomFieldChoiceSet.objects.get(name="Service_commissioning_state"),
            content_types=content_types_ipam
        )

        # Create the 'Deployment_state' custom field
        self.create_or_update_custom_field(
            name='Deployment_state',
            type='select',
            description='The deployment state of the service.',
            choice_set=CustomFieldChoiceSet.objects.get(name="Service_deployment_state"),
            content_types=content_types_ipam
        )

        # Create the 'Iface_mh_mode' custom field
        self.create_or_update_custom_field(
            name='Iface_mh_mode',
            type='select',
            description='Multi Home mode',
            label='Mode',
            group_name="Multi-homing access",
            choice_set=CustomFieldChoiceSet.objects.get(name="MH_mode"),
            content_types=ContentType.objects.get_for_model(Interface)
        )

        # Create the 'Iface_mh_id' custom field
        self.create_or_update_custom_field(
            name='Iface_mh_id',
            type='integer',
            description='Multi Home mode',
            label='ID',
            group_name="Multi-homing access",
            content_types=ContentType.objects.get_for_model(Interface)
        )

        # Create the 'Service_location' custom field
        self.create_or_update_custom_field(
            name='Service_location',
            type='object',
            label='Location',
            description='Service location.',
            content_types=content_types_ipam,
            object_type=ContentType.objects.get_for_model(Location)
        )

        # Create the 'Vrf_wanvrf' custom field
        self.create_or_update_custom_field(
            name='Vrf_wanvrf',
            type='object',
            label='WAN-VRF',
            description='Associates a VRF to WAN VRF.',
            content_types=[ContentType.objects.get_for_model(VRF)],
            object_type=ContentType.objects.get_for_model(VRF)
        )

        # VRF Identifier custom field
        self.create_or_update_custom_field(
            name='Vrf_identifier',
            type='integer',
            label='Identifier',
            description='Identifier for VRF.',
            content_types=[ContentType.objects.get_for_model(VRF)]
        )

        # L2VPN VLAN custom field
        self.create_or_update_custom_field(
            name='L2vpn_vlan',
            type='text',
            label='802.1Q',
            description='VLAN for L2VPN.',
            content_types=[ContentType.objects.get_for_model(L2VPN)],
            validation_regex=r"^(?:untagged|409[0-5]|40[0-8][0-9]|[0-3]?[0-9]{1,3})$"
        )

        # L2VPN Gateway custom field
        self.create_or_update_custom_field(
            name='L2vpn_gateway',
            type='object',
            label='Gateway',
            description='Gateway IP address for L2VPN.',
            group_name="L2VPN VRF association",
            content_types=[ContentType.objects.get_for_model(L2VPN)],
            object_type=ContentType.objects.get_for_model(IPAddress)
        )

        # L2VPN IP VRF custom field
        self.create_or_update_custom_field(
            name='L2vpn_ipvrf',
            type='object',
            label='IP-VRF',
            description='IP VRF for L2VPN.',
            group_name="L2VPN VRF association",
            content_types=[ContentType.objects.get_for_model(L2VPN)],
            object_type=ContentType.objects.get_for_model(VRF)
        )

        # Device ASN custom field
        self.create_or_update_custom_field(
            name='ASN',
            type='object',
            label=None,
            description='Autonomous System Number for devices.',
            content_types=[ContentType.objects.get_for_model(Device)],
            object_type=ContentType.objects.get_for_model(ASN)
        )

        # Site Overlay ASN custom field
        self.create_or_update_custom_field(
            name='Overlay_ASN',
            type='object',
            label=None,
            description='Overlay ASN for locations.',
            choice_set=None,
            content_types=[ContentType.objects.get_for_model(Location)],
            object_type=ContentType.objects.get_for_model(ASN)
        )
