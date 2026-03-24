# Idea

This xApp should show the topology of the IAB network through a dedicate web interface.
The graph, must be constructed starting from the neighbor file, an association gNB-UE (DU-MT) file and a CSV where the data from metrics collection are saved.

Once this is done, I file will be created where e fixed topology has been defined. The idea is to start the network with a random topology, and at a certain point enforce the saved one.

# Deploy
```
docker build --tag iab-xapp:0.0.1 --file docker/Dockerfile.iab_xapp .
docker tag iab-xapp:0.0.1 dpugliese6/iab-xapp:0.0.1
docker push dpugliese6/iab-xapp:0.0.1
cd ric-plt-appmgr/xapp_orchestrater/dev/xapp_onboarder/
. .venv/bin/activate
dms_cli onboard --config-file-path iab_xapp/config/config-file.json --shcema_file_path  iab_xapp/config/schema.json
dms_cli download_helm_chart iab-xapp 0.0.1 
helm install iab-xapp iab-xapp-0.0.1.tgz -n ricxapp
```