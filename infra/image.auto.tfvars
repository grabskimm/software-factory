# The image that runs. Promotion is a change to this one value (a value for the same variable in a -var-file wins over this file).
#
# Terraform loads *.auto.tfvars on its own, so a change here is picked up by the next plan and apply. The value must be an
# image DIGEST, never a tag: `sha256:` followed by 64 lowercase hex characters, as printed by `docker push` or by
#   az acr repository show --name <<ACR_NAME>> --image <<IMAGE_REPOSITORY>>:<<TAG>> --query digest -o tsv
#
# The committed value is a placeholder on purpose. variables.tf rejects it, so a plan fails with a clear message
# until your own digest replaces it. See README.md, "Promote an image digest".

image_digest = "sha256:<<IMAGE_DIGEST_HEX>>"
