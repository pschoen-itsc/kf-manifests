# Copyright 2020-2021 The Kubeflow Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import base64

# From awscli installed in alpine/k8s image
import botocore.session

session = botocore.session.get_session()
iam = session.create_client('iam', region_name='foobar')


def main():
    settings = get_settings_from_env()
    server = server_factory(**settings)
    server.serve_forever()


def get_settings_from_env(controller_port=None,
                          visualization_server_image=None, frontend_image=None,
                          visualization_server_tag=None, frontend_tag=None, disable_istio_sidecar=None):
    """
    Returns a dict of settings from environment variables relevant to the controller

    Environment settings can be overridden by passing them here as arguments.

    Settings are pulled from the all-caps version of the setting name.  The
    following defaults are used if those environment variables are not set
    to enable backwards compatibility with previous versions of this script:
        visualization_server_image: ghcr.io/kubeflow/kfp-visualization-server
        visualization_server_tag: value of KFP_VERSION environment variable
        frontend_image: ghcr.io/kubeflow/kfp-frontend
        frontend_tag: value of KFP_VERSION environment variable
        disable_istio_sidecar: Required (no default)
        minio_access_key: Required (no default)
        minio_secret_key: Required (no default)
    """
    settings = dict()
    settings["controller_port"] = \
        controller_port or \
        os.environ.get("CONTROLLER_PORT", "8080")

    settings["visualization_server_image"] = \
        visualization_server_image or \
        os.environ.get("VISUALIZATION_SERVER_IMAGE", "ghcr.io/kubeflow/kfp-visualization-server")

    settings["frontend_image"] = \
        frontend_image or \
        os.environ.get("FRONTEND_IMAGE", "ghcr.io/kubeflow/kfp-frontend")

    # Look for specific tags for each image first, falling back to
    # previously used KFP_VERSION environment variable for backwards
    # compatibility
    settings["visualization_server_tag"] = \
        visualization_server_tag or \
        os.environ.get("VISUALIZATION_SERVER_TAG") or \
        os.environ["KFP_VERSION"]

    settings["frontend_tag"] = \
        frontend_tag or \
        os.environ.get("FRONTEND_TAG") or \
        os.environ["KFP_VERSION"]

    settings["disable_istio_sidecar"] = \
        disable_istio_sidecar if disable_istio_sidecar is not None \
            else os.environ.get("DISABLE_ISTIO_SIDECAR") == "true"

    return settings


def server_factory(visualization_server_image,
                   visualization_server_tag, frontend_image, frontend_tag,
                   disable_istio_sidecar, url="", controller_port=8080):
    """
    Returns an HTTPServer populated with Handler with customized settings
    """
    class Controller(BaseHTTPRequestHandler):
        def sync(self, parent, attachments):
            # parent is a namespace
            namespace = parent.get("metadata", {}).get("name")

            pipeline_enabled = parent.get("metadata", {}).get(
                "labels", {}).get("pipelines.kubeflow.org/enabled")

            if pipeline_enabled != "true":
                return {"status": {}, "attachments": []}

            # Compute status based on observed state.
            desired_status = {
                "kubeflow-pipelines-ready":
                    len(attachments["Secret.v1"]) == 1 and
                    len(attachments["ConfigMap.v1"]) == 3 and
                    len(attachments["Deployment.apps/v1"]) == 2 and
                    len(attachments["Service.v1"]) == 2 and
                    len(attachments["DestinationRule.networking.istio.io/v1alpha3"]) == 1 and
                    len(attachments["AuthorizationPolicy.security.istio.io/v1beta1"]) == 1 and
                    "True" or "False"
            }

            # Generate the desired attachment object(s).
            desired_resources = [
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "kfp-launcher",
                        "namespace": namespace,
                    },
                    "data": {
                        "defaultPipelineRoot": f"minio://kubeflow/{namespace}/v2/artifacts",
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "metadata-grpc-configmap",
                        "namespace": namespace,
                    },
                    "data": {
                        "METADATA_GRPC_SERVICE_HOST":
                            "metadata-grpc-service.kubeflow",
                        "METADATA_GRPC_SERVICE_PORT": "8080",
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "artifact-repositories",
                        "namespace": namespace,
                        "annotations": {
                            "workflows.argoproj.io/default-artifact-repository": "default-seaweedfs-namespaced"
                        }
                    },
                    "data": {
                        "default-seaweedfs-namespaced": json.dumps({
                            "archiveLogs": True,
                            "s3": {
                                "endpoint": "minio-service.kubeflow:9000",
                                "bucket": "kubeflow",
                                "keyFormat": f"{namespace}/artifacts/{{{{workflow.name}}}}/{{{{workflow.creationTimestamp.Y}}}}/{{{{workflow.creationTimestamp.m}}}}/{{{{workflow.creationTimestamp.d}}}}/{{{{pod.name}}}}",
                                "insecure": True,
                                "accessKeySecret": {
                                    "name": "mlpipeline-minio-artifact",
                                    "key": "accesskey",
                                },
                                "secretKeySecret": {
                                    "name": "mlpipeline-minio-artifact",
                                    "key": "secretkey",
                                }
                            }
                        })
                    }
                },
                # Visualization server related manifests below
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {
                        "labels": {
                            "app": "ml-pipeline-visualizationserver"
                        },
                        "name": "ml-pipeline-visualizationserver",
                        "namespace": namespace,
                    },
                    "spec": {
                        "selector": {
                            "matchLabels": {
                                "app": "ml-pipeline-visualizationserver"
                            },
                        },
                        "template": {
                            "metadata": {
                                "labels": {
                                    "app": "ml-pipeline-visualizationserver"
                                },
                                "annotations": disable_istio_sidecar and {
                                    "sidecar.istio.io/inject": "false"
                                } or {},
                            },
                            "spec": {
                                "containers": [{
                                    "image": f"{visualization_server_image}:{visualization_server_tag}",
                                    "imagePullPolicy":
                                        "IfNotPresent",
                                    "name":
                                        "ml-pipeline-visualizationserver",
                                    "ports": [{
                                        "containerPort": 8888
                                    }],
                                    "resources": {
                                        "requests": {
                                            "cpu": "50m",
                                            "memory": "200Mi"
                                        },
                                        "limits": {
                                            "cpu": "500m",
                                            "memory": "1Gi"
                                        },
                                    }
                                }],
                                "serviceAccountName":
                                    "default-editor",
                            },
                        },
                    },
                },
                {
                    "apiVersion": "networking.istio.io/v1alpha3",
                    "kind": "DestinationRule",
                    "metadata": {
                        "name": "ml-pipeline-visualizationserver",
                        "namespace": namespace,
                    },
                    "spec": {
                        "host": "ml-pipeline-visualizationserver",
                        "trafficPolicy": {
                            "tls": {
                                "mode": "ISTIO_MUTUAL"
                            }
                        }
                    }
                },
                {
                    "apiVersion": "security.istio.io/v1beta1",
                    "kind": "AuthorizationPolicy",
                    "metadata": {
                        "name": "ml-pipeline-visualizationserver",
                        "namespace": namespace,
                    },
                    "spec": {
                        "selector": {
                            "matchLabels": {
                                "app": "ml-pipeline-visualizationserver"
                            }
                        },
                        "rules": [{
                            "from": [{
                                "source": {
                                    "principals": ["cluster.local/ns/kubeflow/sa/ml-pipeline"]
                                }
                            }]
                        }]
                    }
                },
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {
                        "name": "ml-pipeline-visualizationserver",
                        "namespace": namespace,
                    },
                    "spec": {
                        "ports": [{
                            "name": "http",
                            "port": 8888,
                            "protocol": "TCP",
                            "targetPort": 8888,
                        }],
                        "selector": {
                            "app": "ml-pipeline-visualizationserver",
                        },
                    },
                },
                # Artifact fetcher related resources below.
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {
                        "labels": {
                            "app": "ml-pipeline-ui-artifact"
                        },
                        "name": "ml-pipeline-ui-artifact",
                        "namespace": namespace,
                    },
                    "spec": {
                        "selector": {
                            "matchLabels": {
                                "app": "ml-pipeline-ui-artifact"
                            }
                        },
                        "template": {
                            "metadata": {
                                "labels": {
                                    "app": "ml-pipeline-ui-artifact"
                                },
                                "annotations": disable_istio_sidecar and {
                                    "sidecar.istio.io/inject": "false"
                                } or {},
                            },
                            "spec": {
                                "containers": [{
                                    "name":
                                        "ml-pipeline-ui-artifact",
                                    "image": f"{frontend_image}:{frontend_tag}",
                                    "imagePullPolicy":
                                        "IfNotPresent",
                                    "ports": [{
                                        "containerPort": 3000
                                    }],
                                    "env": [
                                        {
                                            "name": "MINIO_ACCESS_KEY",
                                            "valueFrom": {
                                                "secretKeyRef": {
                                                    "key": "accesskey",
                                                    "name": "mlpipeline-minio-artifact"
                                                }
                                            }
                                        },
                                        {
                                            "name": "MINIO_SECRET_KEY",
                                            "valueFrom": {
                                                "secretKeyRef": {
                                                    "key": "secretkey",
                                                    "name": "mlpipeline-minio-artifact"
                                                }
                                            }
                                        }
                                    ],
                                    "resources": {
                                        "requests": {
                                            "cpu": "10m",
                                            "memory": "70Mi"
                                        },
                                        "limits": {
                                            "cpu": "100m",
                                            "memory": "500Mi"
                                        },
                                    }
                                }],
                                "serviceAccountName":
                                    "default-editor"
                            }
                        }
                    }
                },
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {
                        "name": "ml-pipeline-ui-artifact",
                        "namespace": namespace,
                        "labels": {
                            "app": "ml-pipeline-ui-artifact"
                        }
                    },
                    "spec": {
                        "ports": [{
                            "name":
                                "http",  # name is required to let istio understand request protocol
                            "port": 80,
                            "protocol": "TCP",
                            "targetPort": 3000
                        }],
                        "selector": {
                            "app": "ml-pipeline-ui-artifact"
                        }
                    }
                },
            ]
            print('Received request:\n', json.dumps(parent, sort_keys=True))
            print('Desired resources except secrets:\n', json.dumps(desired_resources, sort_keys=True))

            # Moved after the print argument because this is sensitive data.

            # This could lead to the creation of multiple access keys for the same profile
            # which is not an imminent problem, but yet not that elegant. But it is simple
            # and ensures that there is always a secret with valid keys.
            # That is likely why metacontroller docs recommend not having any side effects in the
            # hook function.
            if s3_secret := attachments["Secret.v1"].get(f"{namespace}/mlpipeline-minio-artifact"):
                desired_resources.append(s3_secret)
                print('Using existing secret')
            else:
                print('Creating new access key.')
                s3_access_key = iam.create_access_key(UserName=namespace)
                iam.put_user_policy(
                    UserName=namespace,
                    PolicyName=f"KubeflowProject{namespace}",
                    PolicyDocument=json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [{
                                "Effect": "Allow",
                                "Action": [
                                    "s3:Put*",
                                    "s3:Get*",
                                    "s3:List*"
                                ],
                                "Resource": [
                                    f"arn:aws:s3:::kubeflow/{namespace}/*"
                                ]
                            }
                            ]
                        })
                )
                desired_resources.insert(
                    0,
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {
                            "name": "mlpipeline-minio-artifact",
                            "namespace": namespace,
                        },
                        "data": {
                            "accesskey": base64.b64encode(s3_access_key["AccessKey"]["AccessKeyId"].encode('utf-8')).decode("utf-8"),
                            "secretkey": base64.b64encode(s3_access_key["AccessKey"]["SecretAccessKey"].encode('utf-8')).decode("utf-8"),
                    },
                })

            return {"status": desired_status, "attachments": desired_resources}

        def do_POST(self):
            # Serve the sync() function as a JSON webhook.
            observed = json.loads(
                self.rfile.read(int(self.headers.get("content-length"))))
            desired = self.sync(observed["object"], observed["attachments"])

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(bytes(json.dumps(desired), 'utf-8'))

    return HTTPServer((url, int(controller_port)), Controller)


if __name__ == "__main__":
    main()
