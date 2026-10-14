locals {
  github_oidc_host = "token.actions.githubusercontent.com"

  # One entry per deployment. The key is the deployment's ENV and
  # site_environment (infra/environments/<key>.tfvars). Each deploy role
  # trusts only jobs in its GitHub environment and may manage only resources
  # named soccer-<key>-*, which is what the deployment's root names them
  # (<resource_prefix>-<site_environment>-server); its state is under
  # soccer/<key>/ (infra/environments/<key>.tfbackend). site_role is the one
  # principal the deployment's Function URL may be granted to, the site's
  # role in the same environment (the-lobby).
  environments = {
    dev = {
      github_environment = "dev"
      site_role          = "site-dev-server"
    }
    prod = {
      github_environment = "production"
      site_role          = "site-prod-server"
    }
  }
}
