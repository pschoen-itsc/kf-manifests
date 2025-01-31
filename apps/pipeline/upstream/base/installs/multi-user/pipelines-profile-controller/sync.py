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
import random
import ssl
import string
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import base64
from urllib.error import HTTPError


def main():
    settings = get_settings_from_env()
    server = server_factory(**settings)
    server.serve_forever()


def get_settings_from_env(controller_port=None,
                          visualization_server_image=None, frontend_image=None,
                          visualization_server_tag=None, frontend_tag=None, disable_istio_sidecar=None,
                          kfp_default_pipeline_root=None):
    """
    Returns a dict of settings from environment variables relevant to the controller

    Environment settings can be overridden by passing them here as arguments.

    Settings are pulled from the all-caps version of the setting name.  The
    following defaults are used if those environment variables are not set
    to enable backwards compatibility with previous versions of this script:
        visualization_server_image: gcr.io/ml-pipeline/visualization-server
        visualization_server_tag: value of KFP_VERSION environment variable
        frontend_image: gcr.io/ml-pipeline/frontend
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
        os.environ.get("VISUALIZATION_SERVER_IMAGE", "gcr.io/ml-pipeline/visualization-server")

    settings["frontend_image"] = \
        frontend_image or \
        os.environ.get("FRONTEND_IMAGE", "gcr.io/ml-pipeline/frontend")

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

    # KFP_DEFAULT_PIPELINE_ROOT is optional
    settings["kfp_default_pipeline_root"] = \
        kfp_default_pipeline_root or \
        os.environ.get("KFP_DEFAULT_PIPELINE_ROOT")

    return settings


class SimpleK8sClient:


    def __init__(self):
        self.host = os.environ.get("KUBERNETES_SERVICE_HOST")
        self.port = int(os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS"))
        self.token = open("/var/run/secrets/kubernetes.io/serviceaccount/token").read()
        self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        self.ssl_context.load_verify_locations(
            cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        )

    def request(self, path, method, data=None, headers=None):
        if not headers:
            headers = {}
        base_headers = {"Authorization": f"Bearer {self.token}"}
        headers.update(base_headers)
        req = urllib.request.Request(
            urllib.parse.urljoin(f"https://{self.host}:{self.port}/", path),
            data=data,
            method=method,
            headers=headers
        )
        try:
            with urllib.request.urlopen(req, context=self.ssl_context) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 404:
                return None
            print(e.code, e.read())
            return None

    def get(self, path):
        return self.request(path, "GET")

    def post(self, path, data):
        return self.request(path, "POST", json.dumps(data).encode(), headers={"Content-Type": "application/json"})


def server_factory(visualization_server_image,
                   visualization_server_tag, frontend_image, frontend_tag,
                   disable_istio_sidecar, kfp_default_pipeline_root=None,
                   url="", controller_port=8080):
    """
    Returns an HTTPServer populated with Handler with customized settings
    """
    class Controller(BaseHTTPRequestHandler):
        def sync(self, parent, children):
            # parent is a namespace
            namespace = parent.get("metadata", {}).get("name")

            pipeline_enabled = parent.get("metadata", {}).get(
                "labels", {}).get("pipelines.kubeflow.org/enabled")

            if pipeline_enabled != "true":
                return {"status": {}, "children": []}

            k8s_client = SimpleK8sClient()
            existing_secret = k8s_client.get(f"/api/v1/namespaces/{namespace}/secrets/mlpipeline-minio-artifact")
            if not existing_secret:
                minio_access_key = namespace
                chars = string.ascii_lowercase + string.digits
                minio_secret_key = "".join(random.choice(chars) for _ in range(32))
                seaweed_config_pod_spec = {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "name": f"seaweedfs-config-{namespace}",
                        "namespace": "kubeflow",
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "seaweedfs-config",
                                "image": f"chrislusf/seaweedfs:3.69",
                                "args": [
                                    "shell",
                                    "s3.configure",
                                    f"-user {minio_access_key}",
                                    f"-access_key {minio_access_key}",
                                    f"-secret_key {minio_secret_key}",
                                    f"-actions Read,Write,List",
                                    "-apply",
                                ],
                                "env": [{
                                    "name": "SHELL_FILER",
                                    "value": "minio-service.kubeflow.svc.cluster.local:8888"
                                }, {
                                    "name": "SHELL_MASTER",
                                    "value": "minio-service.kubeflow.svc.cluster.local:9333"
                                }]
                            }
                        ],
                        "serviceAccountName": "seaweedfs",
                        "restartPolicy": "Never",
                    }
                }
                k8s_client.post("/api/v1/namespaces/kubeflow/pods", seaweed_config_pod_spec)
            else:
                minio_access_key = existing_secret["data"]["accesskey"]
                minio_secret_key = existing_secret["data"]["secretkey"]

            desired_configmap_count = 1
            desired_resources = []
            if kfp_default_pipeline_root:
                desired_configmap_count = 2
                desired_resources += [{
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "kfp-launcher",
                        "namespace": namespace,
                    },
                    "data": {
                        "defaultPipelineRoot": kfp_default_pipeline_root,
                    },
                }]


            # Compute status based on observed state.
            desired_status = {
                "kubeflow-pipelines-ready":
                    len(children["Secret.v1"]) == 1 and
                    len(children["ConfigMap.v1"]) == desired_configmap_count and
                    len(children["Deployment.apps/v1"]) == 2 and
                    len(children["Service.v1"]) == 2 and
                    len(children["DestinationRule.networking.istio.io/v1alpha3"]) == 1 and
                    len(children["AuthorizationPolicy.security.istio.io/v1beta1"]) == 1 and
                    "True" or "False"
            }

            # Generate the desired child object(s).
            desired_resources += [
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
            desired_resources.append({
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "mlpipeline-minio-artifact",
                    "namespace": namespace,
                },
                "data": {
                    "accesskey": base64.b64encode(minio_access_key.encode()).decode(),
                    "secretkey": base64.b64encode(minio_secret_key.encode()).decode(),
                },
            })

            return {"status": desired_status, "children": desired_resources}

        def do_POST(self):
            # Serve the sync() function as a JSON webhook.
            observed = json.loads(
                self.rfile.read(int(self.headers.get("content-length"))))
            desired = self.sync(observed["parent"], observed["children"])

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(bytes(json.dumps(desired), 'utf-8'))

    return HTTPServer((url, int(controller_port)), Controller)


if __name__ == "__main__":
    main()
