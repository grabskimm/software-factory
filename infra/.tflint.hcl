# tflint configuration.
#
# Only the Terraform language ruleset that is bundled into the tflint binary is enabled
# (recommended preset), so no plugin download (`tflint --init`) and no network access are
# needed. Bundled plugin and presets:
#   https://raw.githubusercontent.com/terraform-linters/tflint/master/docs/user-guide/plugins.md
#   https://raw.githubusercontent.com/terraform-linters/tflint-ruleset-terraform/main/docs/rules/README.md
#
# The azurerm ruleset is deliberately NOT enabled: it must be downloaded from GitHub at
# `tflint --init` time, so tflint runs here without any download.
# No rule is disabled.

tflint {
  required_version = ">= 0.60.0"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
