#!/bin/bash

# Update script for Docker Compose and services.yaml

# Function to display usage
show_usage() {
    echo "Usage: $0 {up|down|restart|modify}"
    exit 1
}

# Check if at least one argument is provided
if [ "$#" -ne 1 ]; then
    show_usage
fi

# Execute Docker Compose commands based on the input argument
case $1 in
    up)
        docker-compose up -d
        ;;
    down)
        docker-compose down
        ;;
    restart)
        docker-compose restart
        ;;
    modify)
        # Path to services.yaml
        services_yaml="./services.yaml"
        
        # Example modification: Append a new service (customize as needed)
        echo "Modifying services.yaml..."
        echo "\n# Added by update_script.sh\nnew_service:\n  image: example/new_service\n  restart: always\n" >> $services_yaml
        ;;
    *)
        show_usage
        ;;
esac
