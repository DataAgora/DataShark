import secrets
import decimal
import hashlib

import jwt
import boto3
import requests
from flask_cors import CORS
from boto3.dynamodb.conditions import Key
from flask import Flask, request, jsonify


JWT_SECRET = "datajbsnmd5h84rbewvzx6*cax^jgmqw@m3$ds_%z-4*qy0n44fjr5shark"
JWT_ALGO = "HS256"

app = Flask(__name__)
CORS(app)


@app.route("/repo/<repo_id>", methods=["GET"])
def get_repo(repo_id):
    # Check authorization
    claims = authorize_user(request)
    if claims is None: return jsonify(make_unauthorized_error()), 400

    # Get data
    user_id = claims["pk"]
    try:
        repo_details = _get_repo_details(user_id, repo_id)
    except:
        return jsonify(make_error("Error while getting details for repo."))
    return jsonify(repo_details)

@app.route("/repo", methods=["POST"])
def create_new_repo():
    # Check authorization
    claims = authorize_user(request)
    if claims is None: return jsonify(make_unauthorized_error()), 400

    # Get parameters
    # TODO: Sanitize inputs.
    params = request.get_json()
    if "RepoName" not in params:
        return jsonify(make_error("Missing repo name from request.")), 400
    if "RepoDescription" not in params:
        return jsonify(make_error("Missing repo description from request.")), 400
    repo_name = params["RepoName"][:20]
    repo_description = params["RepoDescription"][:80]

    # TODO: Check repo doesn't already exist.

    user_id = claims["pk"]
    try:
        repo_id = _create_new_repo_document(user_id, repo_name, repo_description)
        api_key, true_api_key = _create_new_api_key(user_id, repo_id)
        _update_user_data_with_new_repo(user_id, repo_id, api_key)
        _asynchronously_create_new_cloud_node(repo_id, api_key)
    except Exception as e:
        # TODO: Revert things.
        return jsonify(make_error(str(e))), 400

    return jsonify({
        "error": False,
        "results": {
            "repo_id": repo_id,
            "true_api_key": true_api_key
        }
    })

@app.route("/repos", methods=["GET"])
def get_all_repos():
    # Check authorization
    claims = authorize_user(request)
    if claims is None: return jsonify(make_unauthorized_error()), 400

    # Get data
    user_id = claims["pk"]
    try:
        repo_list = _get_all_repos(user_id)
    except:
        return jsonify(make_error("Error while getting list of repos.")), 400
    return jsonify(repo_list)

@app.route("/logs/<repo_id>", methods=["GET"])
def get_logs(repo_id):
    # Check authorization
    claims = authorize_user(request)
    if claims is None: return jsonify(make_unauthorized_error()), 400

    # Get data
    user_id = claims["pk"]
    try:
        _assert_user_can_read_repo(user_id, repo_id)
        logs = _get_logs(repo_id)
    except Exception as e:
        return jsonify(make_error(str(e))), 400
    return jsonify(logs)

@app.route("/coordinator/status/<repo_id>", methods=["GET"])
def get_coordinator_status(repo_id):
    # Check authorization
    claims = authorize_user(request)
    if claims is None: return jsonify(make_unauthorized_error()), 400

    # Get data
    cloud_node_url = _construct_cloud_node_url(repo_id)
    try:
        # TODO: Remove the 'http' here and add 'https' to the URL constructor.
        r = requests.get("http://" + cloud_node_url + "/status")
        status_data = r.json()
        assert "Busy" in status_data
    except Exception as e:
        return jsonify(make_error("Error while checking coordinator's status.")), 400
    return jsonify(status_data)

# @app.route("/userdata", methods=["POST"])
# def create_user_data():
#     # Check authorization
#     claims = authorize_user(request)
#     if claims is None: return jsonify(make_unauthorized_error()), 400
#
#     # Create document
#     user_id = claims["pk"]
#     try:
#         _create_user_data(user_id)
#     except Exception as e:
#         return jsonify(make_error(str(e))), 400
#     return jsonify({})
#
# def _create_user_data(user_id):
#     """Only creates it if doesn't exist already."""
#     table = _get_dynamodb_table("UsersDashboardData")
#     try:
#         item = {
#             'UserId': user_id,
#             'ReposManaged': set(["null"]),
#             'ApiKeys': set(["null"]),
#             'ReposRemaining': 5,
#         }
#         table.put_item(
#             Item=item,
#             ConditionExpression="attribute_not_exists(UserId)"
#         )
#     except:
#         raise Exception("Error while creating the user data.")

def _assert_user_can_read_repo(user_id, repo_id):
    user_data_table = _get_dynamodb_table("UsersDashboardData")
    try:
        response = user_data_table.get_item(
            Key={
                "UserId": user_id
            }
        )
        user_data = response['Item']
        repos_managed = user_data['ReposManaged']
    except:
        raise Exception("Error while getting user's permissions.")
    print(repos_managed, repo_id)
    assert repo_id in repos_managed, \
        "User doesn't have permissions for this repo."

def _get_logs(repo_id):
    logs_table = _get_dynamodb_table("UpdateStore")
    try:
        response = logs_table.query(
            KeyConditionExpression=Key('RepoId').eq(repo_id)
        )
        logs = response["Items"]
    except Exception as e:
        raise Exception("Error while getting logs for repo. " + str(e))
    return logs

def _get_repo_details(user_id, repo_id):
    repos_table = _get_dynamodb_table("Repos")
    try:
        response = repos_table.get_item(
            Key={
                "Id": repo_id,
                "OwnerId": user_id,
            }
        )
        repo_details = response["Item"]
    except:
        raise Exception("Error while getting repo details.")
    return repo_details

def _update_user_data_with_new_repo(user_id, repo_id, api_key):
    table = _get_dynamodb_table("UsersDashboardData")
    try:
        response = table.update_item(
            Key={
                'UserId': user_id,
            },
            UpdateExpression="SET ReposRemaining = ReposRemaining - :val " + \
                             "ADD ReposManaged :repo_id, " + \
                             "ApiKeys :api_key",
            ExpressionAttributeValues={
                ':val': decimal.Decimal(1),
                ':repo_id': set([repo_id]),
                ':api_key': set([api_key]),
            }
        )
    except:
        raise Exception("Error while updating user data with new repo data.")

def _create_new_repo_document(user_id, repo_name, repo_description):
    table = _get_dynamodb_table("Repos")
    repo_id = secrets.token_hex(16)
    try:
        item = {
            'Id': repo_id,
            'Name': repo_name,
            'Description': repo_description,
            'OwnerId': user_id,
            'ContributorsId': [],
            'CoordinatorAddress': _construct_cloud_node_url(repo_id),
            # 'ExploratoryData': None,
        }
        table.put_item(Item=item)
    except:
        raise Exception("Error while creating the new repo document.")
    return repo_id

def _create_new_api_key(user_id, repo_id):
    table = _get_dynamodb_table("ApiKeys")
    true_api_key = secrets.token_urlsafe(32)
    h = hashlib.sha256()
    h.update(true_api_key.encode('utf-8'))
    api_key = h.hexdigest()
    try:
        item = {
            'Key': api_key,
            'OwnerId': user_id,
            'RepoId': repo_id,
        }
        table.put_item(Item=item)
    except:
        raise Exception("Error while creating a new API key.")
    return api_key, true_api_key

def _get_all_repos(user_id):
    user_data_table = _get_dynamodb_table("UsersDashboardData")
    repos_table = _get_dynamodb_table("Repos")
    try:
        response = user_data_table.get_item(
            Key={
                "UserId": user_id
            }
        )

        user_data = response['Item']
        repos_managed = user_data['ReposManaged']

        all_repos = []
        for repo_id in repos_managed:
            if repo_id == "null": continue
            response = repos_table.get_item(
                Key={
                    "Id": repo_id,
                    "OwnerId": user_id,
                }
            )

            if 'Item' in response:
                all_repos.append(response['Item'])
    except:
        raise Exception("Error while getting all repos.")
    return all_repos

# TO BE IMPLEMENTED.
def _asynchronously_create_new_cloud_node(repo_id, api_key):
    print("Creating new cloud node... (fake)")


def _get_dynamodb_table(table_name):
    dynamodb = boto3.resource('dynamodb', region_name='us-west-1')
    table = dynamodb.Table(table_name)
    return table

def _construct_cloud_node_url(repo_id):
    CLOUD_NODE_ADDRESS_TEMPLATE = "{0}.au4c4pd2ch.us-west-1.elasticbeanstalk.com"
    return CLOUD_NODE_ADDRESS_TEMPLATE.format(repo_id)


def authorize_user(request):
    try:
        jwt_string = request.headers.get("Authorization").split('Bearer ')[1]
        claims = jwt.decode(jwt_string, JWT_SECRET, algorithms=[JWT_ALGO])
    except:
        return None
    return claims

def make_unauthorized_error():
    return make_error('Authorization failed.')

def make_error(msg):
    return {'error': True, 'message': msg}


if __name__ == "__main__":
    app.run(port=5001)