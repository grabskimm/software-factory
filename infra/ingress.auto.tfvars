# Optional, non-secret ingress input (the same pattern as image.auto.tfvars and monitoring-images.auto.tfvars: Terraform loads
# *.auto.tfvars on its own, so a change here is picked up by the next plan).
#
# The host name served as well as the Azure name (README.md, "Browser sign-in and custom host name"). Caddy asks Let's Encrypt for a
# certificate for each name. It needs the DNS record (a CNAME to <ingress_dns_label>.<location>.cloudapp.azure.com, DNS only until
# the certificate is issued) and the single-page-application redirect URI on the Entra app registration.
#
# PLACEHOLDER: factory.example.org is not yours. Replace it with a host name you control, or delete this file (or the line) before a
# real deploy: left as it is, Caddy would ask Let's Encrypt for a certificate for a name that is not yours and fail. A value for the
# same variable in a -var-file wins over this file; set it to null there to serve the Azure name only. The Entra SPA client ID and
# scope are not set here: the dashboard derives them from entra_allowed_audiences.
ingress_custom_host = "factory.example.org"
