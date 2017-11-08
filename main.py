import os
import sys
import time
import logging
from datetime import datetime, timedelta

import boto3

import config


def create_keypair():
    response = ec2_client.create_key_pair(KeyName=config.KEY_NAME)
    key_material = response.get('KeyMaterial')
    with open("%s.pem" %config.KEY_NAME, 'w') as f:
        f.write(key_material)

def create_snapshots(ec2_ids):
    SnapshotId_VolumeId = {}
    for ec2_id in ec2_ids:
        ec2instance = ec2_resource.Instance(ec2_id)
        for device in ec2instance.block_device_mappings:
            volumeId = ec2_id + "_" + device.get('Ebs').get('VolumeId')
            snapshot = ec2_resource.create_snapshot(VolumeId=device.get('Ebs').get('VolumeId'))
            snapshot.create_tags(Resources=[snapshot.id], Tags=[{'Key': config.SNAPSHOT_TAG_KEY, 'Value': config.SNAPSHOT_TAG_VALUE}])
            waiter = ec2_client.get_waiter('snapshot_completed')
            waiter.wait(SnapshotIds=[snapshot.id])
            SnapshotId_VolumeId[snapshot.id] = volumeId

    return SnapshotId_VolumeId


def create_instance():
    create_keypair()

    instances = ec2_resource.create_instances(
        ImageId='ami-1e339e71', MinCount=1, MaxCount=1, InstanceType="t2.small", KeyName=config.KEY_NAME)
    instance = instances[0]
    waiter = ec2_client.get_waiter('instance_running')
    waiter.wait(InstanceIds=[instance.id])
    instance.reload()
    # print ('Instance is running, public IP: {0}'.format(instance.public_ip_address))

    return instance.id


def attach_snapshots(instance_id, SnapshotId_VolumeId):
    instance = ec2_resource.Instance(instance_id)
    # print (instance.block_device_mappings)
    devices_SnapshotId = {}
    i = 0
    for snapshot_id in SnapshotId_VolumeId.keys():
        snapshot = ec2_resource.Snapshot(snapshot_id)
        device = "/dev/sd%s" % chr(i+100)
        volume = ec2_resource.create_volume(SnapshotId=snapshot.id, AvailabilityZone='eu-central-1b', VolumeType='gp2')
        waiter = ec2_client.get_waiter('volume_available')
        waiter.wait(VolumeIds=[volume.id])
        print('Volume is available, volumn ID: %s' % volume.id)
        instance.attach_volume(VolumeId=volume.id, Device=device)
        waiter = ec2_client.get_waiter('volume_in_use')
        waiter.wait(VolumeIds=[volume.id])
        print ('Volume is attached, volumn ID: %s' % volume.id)
        devices_SnapshotId[device] = snapshot_id
        i += 1

    return devices_SnapshotId


def rsync(instance_id, devices_VolumnId):
    result = {'Mount':{}, 'Rsync':{}}

    if not os.path.exists("datastore"):
        os.mkdir('datastore')
    instance = ec2_resource.Instance(instance_id)
    public_dns_name = instance.public_dns_name
    # print (public_dns_name)
    os.system("chmod 400 %s.pem" %config.KEY_NAME)
    try:
        os.system("ssh -o StrictHostKeyChecking=no -i {0} ubuntu@{1} \"sudo mkdir /mnt/datastore\"".format(config.KEY_NAME+".pem",public_dns_name))
    except Exception as e:
        print (e)

    for device, volumeId in devices_VolumnId:
        logging.info('\n\nMounting and RSYNC for %s' % (volumeId))
        try:
            os.system("ssh -o StrictHostKeyChecking=no -i {0} ubuntu@{1} \"sudo mkdir /mnt/datastore/{2}\"".format(
                config.KEY_NAME + ".pem", public_dns_name, volumeId))
        except Exception as e:
            print (e)

        r = os.system("ssh -o StrictHostKeyChecking=no -i {0} ubuntu@{1} \"sudo mount {2} /mnt/datastore/{3}\"".format(
            config.KEY_NAME + ".pem", public_dns_name, device.replace("sd", "xvd")+"1", volumeId))
        if r != 0:
            r = os.system("ssh -o StrictHostKeyChecking=no -i {0} ubuntu@{1} \"sudo mount {2} /mnt/datastore/{3}\"".format(
                config.KEY_NAME + ".pem", public_dns_name, device.replace("sd", "xvd"), volumeId))
            if r != 0:
                logging.error('Mounting: Fail')
                result['Mount'][volumeId] = "Fail"
                continue
            else:
                logging.info('Mounting: Success')
                result['Mount'][volumeId] = "Success"
        else:
            logging.info('Mounting: Success')
            result['Mount'][volumeId] = "Success"

        # RSYNC
        dest = 'datastore/%s' % volumeId
        print ("Starting Rsync from %s:/mnt/datastore/%s to %s" %(public_dns_name, volumeId, dest))
        if not os.path.exists(dest):
            os.mkdir(dest)
        os.system("ssh -o StrictHostKeyChecking=no -i {0} ubuntu@{1} \"sudo chmod -R 777 /mnt/datastore/{2}\"".format(
            config.KEY_NAME + ".pem", public_dns_name, volumeId))
        r = os.system("rsync --delete -azvv -e \"ssh -i {0}\" ubuntu@{1}:/mnt/datastore/{2}/ {3}".format(config.KEY_NAME + ".pem", public_dns_name, volumeId, dest))
        if r == 0:
            logging.info("RSYNC of %s: Success" %volumeId)
            result['Rsync'][volumeId] = "Success"
        else:
            result['Rsync'][volumeId] = "Fail"

    return result


def delete_instance(instance_id):
    try:
        instance = ec2_resource.Instance(instance_id)
        # print (instance.block_device_mappings)
        time.sleep(60)
        volume_ids = [item.get('Ebs').get('VolumeId') for item in instance.block_device_mappings if not item.get('Ebs').get('DeleteOnTermination')]

        instance.terminate()

        waiter = ec2_client.get_waiter('volume_available')
        waiter.wait(VolumeIds=volume_ids)
        for volume_id in volume_ids:
            volume = ec2_resource.Volume(volume_id)
            volume.delete()
    except Exception as e:
        print (e)

    try:
        ec2_client.delete_key_pair(KeyName=config.KEY_NAME)
    except Exception as e:
        print (e)

    try:
        os.remove("%s.pem" % config.KEY_NAME)
    except Exception as e:
        print (e)


def delete_mySnapshots():
    print ("Current Time:",  datetime.utcnow(), "\n")
    delete_time = datetime.utcnow() - timedelta(days=config.DURING_DAYS)
    deletion_counter = 0
    size_counter = 0

    filter = [{'Name': 'tag-value', 'Values': [config.SNAPSHOT_TAG_VALUE]}]
    snapshots = ec2_resource.snapshots.filter(Filters=filter).all()
    for snapshot in snapshots:
        print ("Started Date", snapshot.start_time)
        start_time = snapshot.start_time.replace(tzinfo=None)
        if start_time < delete_time:
            print ('Deleting {id}'.format(id=snapshot.id))
            deletion_counter = deletion_counter + 1
            size_counter = size_counter + snapshot.volume_size
            # Just to make sure you're reading!
            snapshot.delete()
        print ("\n")

    print ('Deleted {number} snapshots totalling {size} GB'.format(number=deletion_counter,size=size_counter))


def main():
    #----- 1.***** create snapshots of the instances.
    print ("\n\n****** 1. Creating the snapshots of the instances")
    SnapshotId_VolumeId = create_snapshots(ec2_ids)
    print ("SnapshotId_VolumeId", SnapshotId_VolumeId)

    #-----2.****** create temporary ubuntu instance
    print("\n\n****** 2. Creating temporay ubuntu instances")
    instance_id = create_instance()
    print ("Instance Id: %s" %instance_id)

    #-----3. attach the snapshots that were create in step1 to the temporary instance of step2.
    print("\n\n****** 3. Attaching the snapshots that were created in step1 to the temporary instance of step2")
    devices_SnapshotId = attach_snapshots(instance_id, SnapshotId_VolumeId)
    print ("devices_SnapshotId", devices_SnapshotId)

    #----4. rsnync from temporary instance to local
    print("\n\n****** 4. rsnync from temporary instance to local. Source: /mnt/datastore, dest: current directory")
    devices_VolumnId = {(key, SnapshotId_VolumeId[value]) for key, value in devices_SnapshotId.items()}
    result = rsync(instance_id, devices_VolumnId)

    # ----5. delete the temporary instance.
    print("\n\n***** 5. Deleting the temorary instance. Instance Id: %s \n\n" % instance_id)
    delete_instance(instance_id)

    # ----6. delete the snapshorts created by this script older than 5 days
    print ("\n\n***** 6. Deleting the snapshorts created by this script older than 5 days ")
    delete_mySnapshots()

    # -----final: Result ---------
    print ("\n\n***** RESULT *****\n")
    print ("-- Mount --")
    print ("\n".join([key+": " + value for key, value in result['Mount'].items()]))
    print ("-- Rsync --")
    print("\n".join([key + ": " + value for key, value in result['Rsync'].items()]))

def partial():
    pass

    # instance_id = 'i-059684d94ee89ddb8'
    # delete_instance(instance_id)
    # delete_mySnapshots()

    # instance = ec2_resource.Instance(instance_id)
    # print (instance.block_device_mappings)

    # public_dns_name = "ec2-18-195-32-63.eu-central-1.compute.amazonaws.com"
    # r = os.system("ssh -o StrictHostKeyChecking=no -i {0} ubuntu@{1} \"sudo mount {2} {3}\"".format(
    #     config.KEY_NAME + ".pem", public_dns_name, '/dev/xvde1', '/mnt/test'))
    # print (type(r))


    # os.system("ssh -o StrictHostKeyChecking=no -i {0} ubuntu@{1} \"sudo chmod -R 777 /mnt/{2}\"".format(
    #     config.KEY_NAME + ".pem", public_dns_name, 'test'))
    # r = os.system(
    #     "rsync --delete -azvv -e \"ssh -i {0}\" ubuntu@{1}:/mnt/{2}/ {3}".format(config.KEY_NAME + ".pem",
    #                                                                             public_dns_name,
    #                                                                             'test',
    #                                                                             'datastore/test'))
    # print (r)

if __name__ == "__main__":
    logging.basicConfig(filename=".log", level=logging.INFO)
    # ec2_ids = ['i-08ae6debac9aa04d7', 'i-06b3453c3b7b31012']
    ec2_client = boto3.client('ec2', config.REGION)
    ec2_resource = boto3.resource('ec2', config.REGION)

    if len(sys.argv) < 2:
        print ("Note: Input the instance ids.\n ex: python main.py i-08ae6debac9aa04d7,i-06b3453c3b7b31012")
        sys.exit(0)
    else:
        ec2_ids = sys.argv[1].split(",")

    main()
    # partial()
    # result = {'Mount': {'0123': 'Fail', '123': 'Success'}, 'Rsync': {'0123': 'Fail', '123': 'Success'}}
    # print("\n".join([key + ": " + value for key, value in result['Mount'].items()]))

