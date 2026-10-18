"""API group, label, annotation, port and default constants.

Names follow upstream KubeRay (v1.7.x) so that manifests can be compared
side by side with the real project.
"""

from .version import DEFAULT_RAY_VERSION

RAY_API_VERSION = "ray.io/v1"
CORE_API_VERSION = "v1"
BATCH_API_VERSION = "batch/v1"
DISCOVERY_API_VERSION = "discovery.k8s.io/v1"
NETWORKING_API_VERSION = "networking.k8s.io/v1"

KIND_RAYCLUSTER = "RayCluster"
KIND_RAYJOB = "RayJob"
KIND_RAYSERVICE = "RayService"
KIND_RAYCronJob = "RayCronJob"
KIND_RAYCRONJOB = "RayCronJob"
KIND_POD = "Pod"
KIND_SERVICE = "Service"
KIND_JOB = "Job"
KIND_EVENT = "Event"
KIND_ENDPOINTSLICE = "EndpointSlice"
KIND_NETWORKPOLICY = "NetworkPolicy"
KIND_PVC = "PersistentVolumeClaim"
KIND_SECRET = "Secret"
KIND_SERVICEACCOUNT = "ServiceAccount"
KIND_ROLE = "Role"
KIND_ROLEBINDING = "RoleBinding"
KIND_HTTPROUTE = "HTTPRoute"
KIND_GATEWAY = "Gateway"
KIND_PODGROUP = "PodGroup"
KIND_INGRESS = "Ingress"

RAY_KINDS = (KIND_RAYCLUSTER, KIND_RAYJOB, KIND_RAYSERVICE, KIND_RAYCRONJOB)

# Resource plural -> kind, used by the REST router and the CLI.
PLURAL_TO_KIND = {
    "pods": KIND_POD,
    "services": KIND_SERVICE,
    "events": KIND_EVENT,
    "jobs": KIND_JOB,
    "endpointslices": KIND_ENDPOINTSLICE,
    "networkpolicies": KIND_NETWORKPOLICY,
    "persistentvolumeclaims": KIND_PVC,
    "secrets": KIND_SECRET,
    "serviceaccounts": KIND_SERVICEACCOUNT,
    "roles": KIND_ROLE,
    "rolebindings": KIND_ROLEBINDING,
    "rayclusters": KIND_RAYCLUSTER,
    "rayjobs": KIND_RAYJOB,
    "rayservices": KIND_RAYSERVICE,
    "raycronjobs": KIND_RAYCRONJOB,
    "httproutes": KIND_HTTPROUTE,
    "gateways": KIND_GATEWAY,
    "podgroups": KIND_PODGROUP,
    "ingresses": KIND_INGRESS,
}

# Labels used by KubeRay.  The controller copies user labels but never lets
# users override the identity labels (cluster/group/node-type).
LABEL_CLUSTER = "ray.io/cluster"
LABEL_NODE_TYPE = "ray.io/node-type"
LABEL_GROUP = "ray.io/group"
LABEL_IS_RAY_NODE = "ray.io/is-ray-node"
LABEL_IDENTIFIER = "ray.io/identifier"
LABEL_SERVE = "ray.io/serve"
LABEL_HEADLESS = "ray.io/headless-worker-svc"
LABEL_ORIGINATED_NAME = "ray.io/originated-from-cr-name"
LABEL_ORIGINATED_CRD = "ray.io/originated-from-crd"
LABEL_CRONJOB_NAME = "ray.io/cronjob-name"
LABEL_JOB_SUBMISSION_MODE = "ray.io/job-submission-mode"
LABEL_KUBERAY_VERSION = "ray.io/kuberay-version"
LABEL_APP_NAME = "app.kubernetes.io/name"
LABEL_APP_COMPONENT = "app.kubernetes.io/component"
LABEL_APP_CREATED_BY = "app.kubernetes.io/created-by"
LABEL_WORKER_REPLICA_NAME = "ray.io/worker-group-replica-name"
LABEL_WORKER_REPLICA_INDEX = "ray.io/worker-group-replica-index"
LABEL_HOST_INDEX = "ray.io/replica-host-index"

NODE_TYPE_HEAD = "head"
NODE_TYPE_WORKER = "worker"
HEAD_GROUP_NAME = "headgroup"

ANNOTATION_FT_ENABLED = "ray.io/ft-enabled"
ANNOTATION_EXTERNAL_STORAGE_NS = "ray.io/external-storage-namespace"
ANNOTATION_OVERWRITE_CMD = "ray.io/overwrite-container-cmd"
ANNOTATION_ENABLE_SERVE_SERVICE = "ray.io/enable-serve-service"
ANNOTATION_INITIALIZING_TIMEOUT = "ray.io/initializing-timeout"
ANNOTATION_CRONJOB_TIMESTAMP = "ray.io/cronjob-scheduled-timestamp"
ANNOTATION_DISABLE_HEAD_RESTART = "ray.io/disable-provisioned-head-restart"
# In-Place Pod Resizing (IPPR).  The first annotation is user-owned; the
# second is written by the autoscaler and consumed by the kubelet.
ANNOTATION_IPPR = "ray.io/ippr"
ANNOTATION_IPPR_STATUS = "ray.io/ippr-status"
# Upstream KubeRay v1.7.1: integer seconds; force-removes the GCS FT
# finalizer when the cleanup Job is stuck.  Default 300s.
ANNOTATION_GCS_FT_DELETION_TIMEOUT = "ray.io/gcs-ft-deletion-timeout"

# Annotations understood by MiniKubeRay's simulated runtime.  They are
# deliberately namespaced away from ray.io so that a real cluster ignores them.
ANNOTATION_SIM_CRASH_AFTER = "minikuberay.io/crash-after-seconds"
ANNOTATION_SIM_JOB_DURATION = "minikuberay.io/job-duration-seconds"
ANNOTATION_SIM_JOB_EXIT_CODE = "minikuberay.io/job-exit-code"
ANNOTATION_SIM_DEPLOY_DELAY = "minikuberay.io/serve-deploy-seconds"
ANNOTATION_SIM_POD_START_DELAY = "minikuberay.io/pod-start-delay-seconds"
ANNOTATION_SIM_JOB_COMPLETE = "minikuberay.io/job-complete-after-seconds"
# Test hook: "succeeded" (default), "failure" or "timeout".
ANNOTATION_SIM_IPPR_RESULT = "minikuberay.io/ippr-result"
ANNOTATION_SIM_IPPR_DELAY = "minikuberay.io/ippr-delay-seconds"
ANNOTATION_SPEC_HASH = "minikuberay.io/spec-hash"
ANNOTATION_APPLIED_CLUSTER_CONFIG = "minikuberay.io/applied-cluster-config"

# Finalizers.
FINALIZER_RAYJOB = "ray.io/rayjob-finalizer"
FINALIZER_RAYSERVICE = "ray.io/rayservice-finalizer"
FINALIZER_GCS_FT = "ray.io/gcs-ft-redis-cleanup-finalizer"
FINALIZER_RAYCLUSTER = "ray.io/raycluster-finalizer"

# Environment variable names injected into Ray Pods.  Values match KubeRay.
RAY_CLUSTER_NAME = "RAY_CLUSTER_NAME"
RAY_CLUSTER_NAMESPACE = "RAY_CLUSTER_NAMESPACE"
RAY_NODE_TYPE = "RAY_NODE_TYPE"
RAY_IP = "RAY_IP"
FQ_RAY_IP = "FQ_RAY_IP"
RAY_PORT = "RAY_PORT"
RAY_ADDRESS = "RAY_ADDRESS"
RAY_REDIS_ADDRESS = "RAY_REDIS_ADDRESS"
REDIS_PASSWORD = "REDIS_PASSWORD"
REDIS_USERNAME = "REDIS_USERNAME"
RAY_DASHBOARD_ENABLE_K8S_DISK_USAGE = "RAY_DASHBOARD_ENABLE_K8S_DISK_USAGE"
RAY_EXTERNAL_STORAGE_NS = "RAY_external_storage_namespace"
RAY_GCS_STORAGE = "RAY_gcs_storage"
RAY_GCS_STORAGE_PATH = "RAY_gcs_storage_path"
RAY_GCS_RPC_RECONNECT_TIMEOUT_S = "RAY_gcs_rpc_server_reconnect_timeout_s"
RAY_ENABLE_AUTOSCALER_V2 = "RAY_enable_autoscaler_v2"
KUBERAY_GEN_RAY_START_CMD = "KUBERAY_GEN_RAY_START_CMD"
RAY_START_ULIMIT_OPEN_FILES = "RAY_START_ULIMIT_OPEN_FILES"
RAY_USE_TLS = "RAY_USE_TLS"
RAY_TLS_SERVER_CERT = "RAY_TLS_SERVER_CERT"
RAY_TLS_SERVER_KEY = "RAY_TLS_SERVER_KEY"
RAY_TLS_CA_CERT = "RAY_TLS_CA_CERT"
RAY_AUTH_MODE = "RAY_AUTH_MODE"
RAY_AUTH_TOKEN = "RAY_AUTH_TOKEN"
RAY_ENABLE_K8S_TOKEN_AUTH = "RAY_ENABLE_K8S_TOKEN_AUTH"
RAY_DASHBOARD_ADDRESS = "RAY_DASHBOARD_ADDRESS"
RAY_JOB_SUBMISSION_ID = "RAY_JOB_SUBMISSION_ID"
RAY_CLOUD_INSTANCE_ID = "RAY_CLOUD_INSTANCE_ID"
RAY_NODE_TYPE_NAME = "RAY_NODE_TYPE_NAME"

# Ports from KubeRay's constants.go.
PORT_GCS = 6379
PORT_DASHBOARD = 8265
PORT_CLIENT = 10001
PORT_METRICS = 8080
PORT_DASHBOARD_AGENT = 52365
PORT_SERVE = 8000
PORT_HTTP = 80

PORT_NAMES = {
    PORT_CLIENT: "client",
    PORT_GCS: "gcs-server",
    PORT_DASHBOARD: "dashboard",
    PORT_METRICS: "metrics",
    PORT_SERVE: "serve",
}

DEFAULT_IMAGE = f"rayproject/ray:{DEFAULT_RAY_VERSION}"
DEFAULT_GPU_IMAGE = f"rayproject/ray:{DEFAULT_RAY_VERSION}-gpu"
DEFAULT_AUTOSCALER_IMAGE = DEFAULT_IMAGE
DEFAULT_COLLECTOR_IMAGE = "quay.io/kuberay/collector:v1.7.1"

DEFAULT_NAMESPACE = "default"
DEFAULT_CLUSTER_DOMAIN = "cluster.local"

# KubeRay default timing values.
DEFAULT_REQUEUE_SECONDS = 2
DEFAULT_RESYNC_SECONDS = 300
RAYJOB_DEFAULT_REQUEUE_SECONDS = 2
RAYSERVICE_DEFAULT_REQUEUE_SECONDS = 2
RAYCronJob_DEFAULT_REQUEUE_SECONDS = 3
RAYCRONJOB_DEFAULT_REQUEUE_SECONDS = RAYCronJob_DEFAULT_REQUEUE_SECONDS
DEFAULT_IDLE_TIMEOUT_SECONDS = 60
DEFAULT_RAYCLUSTER_DELETION_DELAY_SECONDS = 60
DEFAULT_POD_START_DELAY_SECONDS = 0.2
DEFAULT_JOB_DURATION_SECONDS = 1.0
DEFAULT_SERVE_DEPLOY_SECONDS = 0.5
# Simulated kubelet latency for an in-place resize.  Real resizes are async
# and may take seconds; a small deterministic value keeps tests fast.
DEFAULT_IPPR_RESIZE_SECONDS = 0.2

MAX_CLUSTER_NAME_LENGTH = 53
MAX_SERVICE_NAME_LENGTH = 63
MAX_LABEL_VALUE_LENGTH = 63
MAX_INT32 = 2_147_483_647

MANAGED_BY_KUBERAY = "ray.io/kuberay-operator"
MANAGED_BY_MULTIKUEUE = "kueue.x-k8s.io/multikueue"

UPSCALING_MODES = ("Default", "Aggressive", "Conservative")
AUTOSCALER_VERSIONS = ("v1", "v2")
SUBMISSION_MODES = ("K8sJobMode", "HTTPMode", "InteractiveMode", "SidecarMode")
RAY_SERVICE_UPGRADE_TYPES = ("NewCluster", "NewClusterWithIncrementalUpgrade", "None")
RAY_CLUSTER_UPGRADE_TYPES = ("Recreate", "None")
DELETION_POLICIES = ("DeleteCluster", "DeleteWorkers", "DeleteSelf", "DeleteNone")
GCS_FT_BACKENDS = ("redis", "rocksdb")
NETWORK_POLICY_MODES = ("DenyAll", "DenyAllIngress", "DenyAllEgress")
AUTH_MODES = ("disabled", "token")
BATCH_SCHEDULERS = ("volcano", "yunikorn", "scheduler-plugins", "was")
BATCH_SCHEDULER_NAMES = {
    "volcano": "volcano",
    "yunikorn": "yunikorn",
    "scheduler-plugins": "scheduler-plugins-scheduler",
    "was": "scheduler-plugins-scheduler",
}
QUEUE_LABELS = (
    "volcano.sh/queue-name",   # KubeRay's Volcano integration
    "yunikorn.apache.org/queue",
    "ray.io/queue",            # legacy alias kept for older manifests
    "volcano.sh/queue",        # legacy alias
)

# Job statuses and deployment statuses (RayJob).
JOB_STATUS_NEW = ""
JOB_STATUS_PENDING = "PENDING"
JOB_STATUS_RUNNING = "RUNNING"
JOB_STATUS_STOPPED = "STOPPED"
JOB_STATUS_SUCCEEDED = "SUCCEEDED"
JOB_STATUS_FAILED = "FAILED"
TERMINAL_JOB_STATUSES = (JOB_STATUS_STOPPED, JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED)

DEPLOYMENT_NEW = ""
DEPLOYMENT_INITIALIZING = "Initializing"
DEPLOYMENT_RUNNING = "Running"
DEPLOYMENT_COMPLETE = "Complete"
DEPLOYMENT_FAILED = "Failed"
DEPLOYMENT_VALIDATION_FAILED = "ValidationFailed"
DEPLOYMENT_SUSPENDING = "Suspending"
DEPLOYMENT_SUSPENDED = "Suspended"
DEPLOYMENT_RETRYING = "Retrying"
DEPLOYMENT_WAITING = "Waiting"

# Condition types.
COND_PROVISIONED = "RayClusterProvisioned"
COND_HEAD_READY = "HeadPodReady"
COND_REPLICA_FAILURE = "ReplicaFailure"
COND_SUSPENDING = "RayClusterSuspending"
COND_SUSPENDED = "RayClusterSuspended"
COND_READY = "Ready"
COND_UPGRADE_IN_PROGRESS = "UpgradeInProgress"
COND_ROLLBACK_IN_PROGRESS = "RollbackInProgress"
COND_SERVICE_SUSPENDING = "Suspending"
COND_SERVICE_SUSPENDED = "Suspended"
COND_JOB_SUBMISSION = "JobSubmission"
COND_JOB_DEPLOYMENT = "JobDeployment"
COND_JOB_FINISHED = "JobFinished"
