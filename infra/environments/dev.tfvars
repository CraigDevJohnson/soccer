# The dev deployment, soccer-dev-server, for https://dev.craigdevjohnson.com.
# The Deploy workflow applies this file on every push to main that changes the
# backend, and `task infra:plan` reads it on a computer, so it is committed:
# a value kept anywhere else would be undone by the next deploy. Nothing here
# is secret.

account_id       = "793680745829" # the workloads account
site_environment = "dev"

# The site this backend serves: its Lambda role (the only caller the Function
# URL accepts) and where it serves Session links.
site_role_arn = "arn:aws:iam::793680745829:role/site-dev-server"
link_base_url = "https://dev.craigdevjohnson.com/soccer/link/"

# The account's alerts topic (aws-design.md section 7); the alarms notify it.
alert_topic_arn = "arn:aws:sns:us-west-2:793680745829:alerts"

# Made by infra/bootstrap. The deploy role may only create or change the
# function's role while it carries this boundary.
permissions_boundary_arn = "arn:aws:iam::793680745829:policy/soccer-server-boundary-dev"

# league_base_url and league_facility_id stay unset, so the code's defaults
# (Boise, at the league's service the code knows; README) apply.

# The site's cost tag, so its budget covers this backend too.
tags = { Site = "craigdevjohnson.com" }
