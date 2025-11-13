#!/bin/bash

# Update script for gathering Docker container information and modifying services.yaml

# Function to gather information about Docker containers and update services.yaml
update_services() {
    # Command to fetch all container info (running, stopped, and failed)
    containers=$(docker ps -a --format "{{.Names}} - {{.Status}}")

    # Path to the services.yaml file
    services_yaml="./services.yaml"

    echo "Updating services.yaml with the following container information:"\n    echo "$containers"

    # Add container info to services.yaml without overwriting existing entries
    echo "\n# Added by update_script.sh" >> $services_yaml
    while IFS= read -r container; do
        echo "- $container" >> $services_yaml
    done <<< "$containers"
}

# Function to display usage
show_usage() {
    echo "Usage: $0 update"
    exit 1
}

# Check if the correct argument is provided
if [ "$#" -ne 1 ] || [ "$1" != "update" ]; then
    show_usage
fi

# Run the update function
update_services
