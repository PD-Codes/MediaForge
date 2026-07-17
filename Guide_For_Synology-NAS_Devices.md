# Tutorial for Synology NAS Devices

## Prereq:
- Web Station is installed
- SSH Console is enabled


## Prereq Folders:
1. Open File Station
2. Create a Folder in the docker folder of the NAS System with Name "mf-downloads"
3. Create a Folder in the docker folder of the NAS System with Name "mf-data"
4. Create a Folder in the docker folder of the NAS System with Name "mf-tmp"
5. In SSH execute these commands:
```
sudo chown 1000:1000 /volume1/docker/mf-data
sudo chown 1000:1000 /volume1/docker/mf-downloads
sudo chown 1000:1000 /volume1/docker/mf-tmp
sudo chmod ug=rwx /volume1/docker/mf-data
sudo chmod ug=rwx /volume1/docker/mf-downloads
sudo chmod ug=rwx /volume1/docker/mf-tmp
```
## Setup:
1. Login via SSH on Synology Device
2. run command: docker pull ghcr.io/pd-codes/mediaforge:latest
3. In DSM open Container Manager, go to Image, select the mediaforge Image and click on Run
4. Enable checkbox for "Set up web portal via Web Station"
5. Click on Next and choose an open Port on your local device (if this is the only package, you can choose Port 8080)
6. Add Folder in Volume Settings from Step 2 of "Prereq Folder" with target "/app/Downloads"
7. Add Folder in Volume Settings from Step 3 of "Prereq Folder" with target "/home/mediaforge/.mediaforge"
8. Add Folder in Volume Settings from Step 4 of "Prereq Folder" with target "/dev/shm"
9. Click on next and create the container
10. In Web Station Setup change Portal Type from "Name-based" to "Port-based", select HTTP and select the Port 8080