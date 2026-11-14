# The monitoring images, pinned by DIGEST (a tag is kept beside it for the reader; Kubernetes pulls the
# digest). Terraform loads *.auto.tfvars on its own; your own -var-file overrides these values, and
# variables.tf holds the same versions as digest-less defaults.
#
# How the digests were taken (README.md, "Monitoring"): the version tags were listed on the registry and
# the digest of the multi-architecture index of each was read from the registry API
# (the Docker-Content-Digest header of GET /v2/<repository>/manifests/<tag>). To move to another
# version, change both the tag and the digest here in a reviewed pull request.

prometheus_image   = "docker.io/prom/prometheus:v3.15.1@sha256:fa3302279710be2f21a8301ec17a8b4f2301fd829bd113839b3b6b02b6f191a2"
grafana_image      = "docker.io/grafana/grafana:13.2.5@sha256:9f7aa1e1e3246aade5792d35a738049f7f32f89cd51d1956cf426a4e86268a84"
alertmanager_image = "docker.io/prom/alertmanager:v0.34.2@sha256:39d8bb3966c694a124428a3a88fbcffd40bd069129ecd47c059f8a3f17c55f3b"

# kube-state-metrics (issue 149): the digest is of the multi-architecture index (docker buildx imagetools inspect, 2026-11-10).
kube_state_metrics_image = "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.15.0@sha256:db384bf43222b066c378e77027a675d4cd9911107adba46c2922b3a55e10d6fb"
