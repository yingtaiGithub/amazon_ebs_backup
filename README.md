The script was built with python3 on ubuntu 16.04

The script do the followings.
- Input: a list of EC2 instance IDs (can be configured in the script, all using EBS storage)

1. Create a snapshot of all EBS volumes mounted in the instances

2. Create an temporary small Ubuntu 16.04 instance

3. Attach all snapshots that were just created to the temporary instance

4. Run rsync to copy the content of the snapshots to our local machine
   Source: /mnt/datastore
   Destination: The directory of the project.

5. Destroy the temporary instance

6. Delete all snapshots created by the backup script older than 5 days


## How to run?
1. Install dependencies
    pip install boto3
2. Configuring the constants in config.py
3. Run the script.
    python main.py [instance_id1],[instance_id2],...
    ex: python main.py i-08ae6debac9aa04d7,i-06b3453c3b7b31012

    Note: The space mustn't exists between 'instance_id' and 'instance_id'
          i-08ae6debac9aa04d7,i-06b3453c3b7b31012 -> Ok
          i-08ae6debac9aa04d7, i-06b3453c3b7b31012 -> Fail