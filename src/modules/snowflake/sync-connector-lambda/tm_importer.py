#!/usr/bin/env python

import argparse
import csv
import json
import os
import re

from library import * ## Common entity construction methods.

'''
Read output of a json iottwinmaker structure and create iottwinmaker entities
input:
    -b  --bucket            The bucket containing exported osipi data
    -p  --prefix            The path to JSON file in s3 containing exported osipi data
    -c  --component-type-id The component id to create all properties under.
    -w  --workspace-id      Workspace id that will be created.
    -r  --iottwinmaker-role-arn     The ARN of the role which will be assumed by Iottwinmaker

output:
    None on console, creates entities in Iottwinmaker workspace
'''

#Caches
created_component_types = dict()
created_entities = dict()

def get_iottwinmaker_client():
    #load_env()
    iottwinmaker = boto3_session().client('iottwinmaker')
    return iottwinmaker

SERVICE_ENDPOINT= os.environ.get('AWS_ENDPOINT')
s3 = boto3_session().client('s3')
iottwinmaker_client = get_iottwinmaker_client()

def parse_arguments():
  parser = argparse.ArgumentParser(
                  description='Load JSON entities into Iottwinmaker')
  parser.add_argument('-b', '--bucket',
                        help='The bucket containing exported osipi data',
                        required=True)
  parser.add_argument('-p', '--prefix',
                        help='The path to JSON file in s3 containing exported osipi data',
                        required=True)
  parser.add_argument('-w', '--workspace-id',
                        help='The workspace id to create components and entities in',
                        required=True)
  parser.add_argument('-c', '--component-type-id',
                        help='The component id to create all properties under.',
                        required=True)
  parser.add_argument('-r', '--iottwinmaker-role-arn',
                        help='ARN of the role assumed by Iottwinmaker',
                        default=False,
                        required=False)
  return parser

def create_properties_component(workspace_id, comp_id):
    if comp_id in created_component_types:
        log(f"Component type {comp_id} already created, skipping.")
        return
    if not comp_id:
        return
    log("API call: list component types")
    cs = iottwinmaker_client.list_component_types(workspaceId = workspace_id)
    for c in cs.get('componentTypeSummaries'):
        if comp_id and comp_id == c.get("componentTypeId"):
            created_component_types[comp_id] = 1
            return
    log(f"API call: create component type: {comp_id}")
    resp = iottwinmaker_client.create_component_type(
            workspaceId = workspace_id,
            componentTypeId = comp_id,
            propertyDefinitions = {
                "attributes": {
                    "dataType": {
                        "type": "STRING"
                    },
                    "isTimeSeries": False,
                    "isRequiredInEntity": False
                }
            }
        )
    api_report(resp)
    log(f"API call: get component type: {comp_id}")
    wait_over(iottwinmaker_client.get_component_type,
                {"componentTypeId":comp_id, "workspaceId":workspace_id},
                'status.state', 'ACTIVE')
    created_component_types[comp_id] = 1

#def create_workspace(workspace_id):
def create_workspace(workspace_id, iottwinmaker_role_arn):
    log(f"API call: list workspaces")
    ws = iottwinmaker_client.list_workspaces()
    for w in ws.get("workspaceSummaries"):
        if workspace_id == w.get("workspaceId"):
            return
    bucket_name = "iottwinmaker-" + workspace_id
    s3.create_bucket(Bucket=bucket_name)
    bucket_created = s3.get_waiter('bucket_exists')
    bucket_created.wait(Bucket=bucket_name)

    iot_role = iottwinmaker_role_arn if iottwinmaker_role_arn else get_role_from_identity()
    log(f"API call: create workspace: {workspace_id}")
    resp = iottwinmaker_client.create_workspace(
            workspaceId = workspace_id,
            s3Location = 'arn:aws:s3:::' + bucket_name,
            role = iot_role
    )
    api_report(resp)

def populate_assets(entity, comp_id, workspace_id):
    create_properties_component(workspace_id, comp_id)
    description = entity.get("description")
    assets = entity.get("template_parameters",[])
    properties = entity.get("properties",{})
    components = {
            "attributes": {
                "componentTypeId" : comp_id,
                "properties" : properties
            }
        }
    return components

def entity_exists(workspace_id, entity_id):
    #Check cache first
    if entity_id in created_entities:
        return True
    try:
        log(f"API call: get entity: {entity_id}")
        resp = iottwinmaker_client.get_entity(
            workspaceId = workspace_id,
            entityId = entity_id)
        api_report(resp)
    except:
        return False        
    created_entities[entity_id] = 1 
    return True

def get_parent_from_input(input_entities, parent_id):
    for entity in input_entities:
        if parent_id == entity.get("entity_id"):
            return entity
    return None


def create_root(root_id, root_name, workspace_id):
    if root_name == '$ROOT':
        root_name = 'ROOT'
    return {
        "entityId": root_id,
        "entityName": root_name,
        "description": root_name,
        "workspaceId": workspace_id
    }


def create_iottwinmaker_entity(input_entities, entity, workspace_id, comps, check_active = False):
    entity_id = entity.get("entity_id")
    parent_id = entity.get("parent_entity_id")
    entity_name = entity.get("entity_name")
    parent_name = entity.get("parent_name")
    comp_id = entity.get("component_type")
    description = entity.get("description") if entity.get("description") else entity_name

    log(f"Processing entity {entity_id}, parent {parent_id}")

    if entity_exists(workspace_id, entity_id):
        log(f"Entity {entity_id} already exists, not creating again")
        return

    comps = populate_assets(entity, comp_id, workspace_id) if comp_id else comps

    if parent_id is not None:
        parent_exists = entity_exists(workspace_id, parent_id)
        if not parent_exists:
            parent = get_parent_from_input(input_entities, parent_id)
            if parent is not None:
                create_iottwinmaker_entity(input_entities, parent, workspace_id, comps, True)
            else:
                root = create_root(parent_id, parent_name, workspace_id)
                create_entity_api(comps, root, workspace_id, True)
    else:
        parent_id = '$ROOT'

    ntt = { "entityName": entity_name,
        "entityId": entity_id,
        "parentEntityId": parent_id,
        "description": description,
        "workspaceId": workspace_id }

    create_entity_api(comps, ntt, workspace_id, check_active)


def create_entity_api(comps, ntt, workspace_id, check_active = False):
    log(f"API call: create entity: {ntt['entityId']}")
    if comps:
        resp = iottwinmaker_client.create_entity(
            **ntt, components=comps)
    else:
        resp = iottwinmaker_client.create_entity(**ntt)
    api_report(resp)
    if check_active:
        log(f"API call: get entity: {ntt['entityId']}")
        wait_over(iottwinmaker_client.get_entity,
                {"entityId": ntt.get('entityId'), "workspaceId": workspace_id},
                'status.state', 'ACTIVE')




def show_entity(entity):
    log(str(entity))

def process_records(j_data, workspace_id, comp_id):
    entities = j_data.get("entities")
    for entity in entities:
        comps = populate_assets(entity, comp_id, workspace_id)
        #create_iottwinmaker_entity( entity, workspace_id )
        create_iottwinmaker_entity( entities, entity, workspace_id, comps )

def create_iottwinmaker_entities(j_data, workspace_id, comp_id, iottwinmaker_role_arn):
    create_workspace(workspace_id, iottwinmaker_role_arn)
    create_properties_component(workspace_id, comp_id)
    process_records(j_data, workspace_id, comp_id)
    #process_records(j_data, workspace_id)
    
def import_handler(event, context):
    #load_env()
    input = event.get('body')
    json_bucket = input.get("outputBucket")
    json_file = input.get("outputPath")
    workspace_id = input.get("workspaceId")
    connector = input.get("componentTypeId")
    iottwinmaker_role_arn = input.get("iottwinmakerRoleArn")
    obj_content = s3.get_object(Bucket = json_bucket, Key = json_file)
    file_content = obj_content['Body'].read().decode('utf-8')
    json_content = json.loads(file_content)
    create_iottwinmaker_entities(json_content, workspace_id, connector, iottwinmaker_role_arn)

def main():
    if __name__ != '__main__':
        return
    parser = parse_arguments()
    args = parser.parse_args()
    #Push logs to stdout as well
    logging.getLogger().addHandler(logging.StreamHandler())
    log("Starting import...")
    import_handler( {'body':{
                'outputBucket':args.bucket,
                'outputPath':args.prefix,
                'workspaceId':args.workspace_id,
                'componentTypeId':args.component_type_id,
                'iottwinmakerRoleArn' : args.iottwinmaker_role_arn}}, None)

main()
