# NetBox Custom Scripts for Nokia SRL

This repository contains a collection of custom scripts for NetBox designed to automate various networking tasks specifically for Nokia Service Router Linux (SRL). The scripts cover a range of functionalities from initial NetBox setup, infrastructure configuration, to services deployment, integrating tightly with both manual and automated workflows.

## Scripts Overview

### NetBox Initialization
- `1_NetboxInit.py`: Initializes NetBox with pre-defined device roles, platforms, configuration contexts, and custom fields to support Nokia SRL devices and services.

### Infrastructure Configuration
- `2_Infrastructure.py`: 
    - `ImportFabricFromYAML`: Imports a network fabric configuration from a YAML file, creating devices, interfaces, and setting up ASNs.
    - `BulkImportLAGsFromYAML`: Imports Link Aggregation Groups (LAGs) and their configurations from a YAML file.
    - `CreateLag`: Guides through creating or updating a multihome Lag with specified member interfaces.
    - `DeleteLag`: Allows for the safe deletion of a specified multihome LAG and disassociates its member interfaces.
    - `Create Fabric`: Automated Fabric Creation

### Services Deployment
- `3_Services.py`: 
    - `L2VPNsBulkImport`: Creates or updates L2VPN instances based on YAML input.
    - `CreateL2VPN`: Creates or updates a single L2VPN instance with detailed options.
    - `DeleteL2VPN`: Safely deletes a selected L2VPN instance and its associated resources.
    - `VRFsBulkImport`: Creates or updates VRFs based on YAML input.
    - `CreateVRF`: Creates or updates a VRF instance, linking to related MAC-VRFs.
    - `DeleteVRF`: Deletes a selected VRF instance and its associated resources.
    - `ListVPNs`: Lists all VPN instances with their details and associated interfaces.

## Usage

To use these scripts, ensure you have a running instance of NetBox and add these scripts according to the NetBox documentation. Scripts can be run directly from the NetBox interface by navigating to the scripts page, selecting a script, filling in the required fields, and executing the script.

## Requirements

- NetBox v3.x or later
- Python 3.6 or newer for script execution

## Note

For scripts that utilize YAML files for importing configurations (`ImportFabricFromYAML`, `BulkImportLAGsFromYAML`, `L2VPNsBulkImport`, `VRFsBulkImport`), ensure the YAML structure matches the expected format detailed in each script's description. See intents folder for examples.
