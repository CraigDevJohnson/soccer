mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/mock" }
  }
}

variables {
  account_id       = "111122223333"
  site_environment = "prod"
}

run "protected_production_preview_cannot_write" {
  command = plan
  assert {
    condition = (
      toset(keys(aws_iam_role.routine)) == toset(["infra", "infra-plan"]) &&
      jsondecode(aws_iam_role.routine["infra"].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:CraigDevJohnson@42454849/soccer@1412575682:environment:archive-infra-prod" &&
      jsondecode(aws_iam_role.routine["infra-plan"].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:CraigDevJohnson@42454849/soccer@1412575682:environment:archive-infra-plan-prod" &&
      alltrue(flatten([for s in jsondecode(aws_iam_role_policy.routine_reads["infra-plan"].policy).Statement : [
        for a in s.Action : can(regex(":(Get|List|Describe)", a))
      ]])) &&
      jsondecode(aws_iam_role_policy.routine_reads["infra-plan"].policy).Statement[1].Resource == "arn:aws:s3:::craigdevjohnson-tofu-state-111122223333/soccer-archive/prod/terraform.tfstate"
    )
    error_message = "Only the protected production apply identity may have write access; the preview reads its own state."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.routine_writes.policy).Statement[0].Resource == "arn:aws:s3:::craigdevjohnson-tofu-state-111122223333/soccer-archive/prod/terraform.tfstate" &&
      jsondecode(aws_iam_role_policy.routine_writes.policy).Statement[1].Resource == "arn:aws:s3:::craigdevjohnson-tofu-state-111122223333/soccer-archive/prod/terraform.tfstate.tflock" &&
      alltrue([for s in jsondecode(aws_iam_role_policy.routine_writes.policy).Statement :
        s.Resource != "*" && !strcontains(jsonencode(s.Resource), "soccer-archive-dev") && !strcontains(jsonencode(s.Resource), "recovery")
      ]) &&
      alltrue(flatten([for s in jsondecode(aws_iam_role_policy.routine_writes.policy).Statement : [
        for a in s.Action : !startswith(a, "iam:") || (
          a == "iam:PassRole" && try(s.Condition.StringEquals["iam:PassedToService"] == "scheduler.amazonaws.com", false) &&
          s.Resource == "arn:aws:iam::111122223333:role/soccer-archive-prod-scheduler"
        )
      ]]))
    )
    error_message = "The deploy role must keep its own state, constrain writes to this environment and never modify or pass unrelated roles."
  }
}

run "dev_has_no_prod_preview_identity" {
  command = plan
  variables { site_environment = "dev" }
  assert {
    condition     = toset(keys(aws_iam_role.routine)) == toset(["infra"]) && !strcontains(aws_iam_role_policy.routine_writes.policy, "soccer-archive-prod")
    error_message = "The dev deploy role must remain isolated from production."
  }
}
