#!/usr/bin/env bash
# Checks that the dev deploy role's alarm guard covers every action list an
# alarm has. The role may call PutMetricAlarm only while cloudwatch:AlarmActions
# names nothing but the alerts topic (infra/bootstrap, AlarmsNotifyOnlyAlerts).
# AWS describes that key as the actions an alarm takes when its state changes;
# this checks it also holds an alarm's OK and insufficient-data actions, which
# can name the same EC2, Lambda and Systems Manager actions an ALARM action can.
#
# Run by the Check alarm guard workflow as soccer-deployer-dev, after Craig
# approves the run (docs/operations/deploy.md). Five PutMetricAlarm calls on
# one throwaway alarm, differing only in their action lists:
#   P   alarm actions: alerts                         must be allowed (control)
#   N   alarm actions: decoy                          must be refused (control)
#   T1  alarm actions: alerts, OK actions: decoy      refused if the key covers OK actions
#   T2  alarm actions: alerts, insufficient: decoy    refused if it covers insufficient-data actions
#   T3  OK actions: decoy, nothing else               refused if a bare OK action is caught too
# The alarm watches a metric nothing publishes, with missing data treated as
# not breaching, so it never reaches ALARM and never notifies the alerts
# topic. The decoy is a well-formed SNS topic ARN in this account and Region
# that no topic has, so an alarm made by mistake notifies nobody. Any call
# that does make the alarm is followed by deleting it.
set -uo pipefail

NAME=soccer-dev-alarm-guard-check
ACCOUNT=793680745829
REGION=us-west-2
ALERTS="arn:aws:sns:${REGION}:${ACCOUNT}:alerts"
DECOY="arn:aws:sns:${REGION}:${ACCOUNT}:soccer-dev-alarm-guard-decoy"
export AWS_REGION=$REGION AWS_DEFAULT_REGION=$REGION AWS_PAGER=""

caller=$(aws sts get-caller-identity --query Arn --output text) || exit 1
echo "Signed in as $caller"
case $caller in
  "arn:aws:sts::${ACCOUNT}:assumed-role/soccer-deployer-dev/"*) ;;
  *)
    echo "::error::Not signed in as soccer-deployer-dev; this check proves nothing as anyone else" >&2
    exit 1
    ;;
esac

# Prints how many metric or composite alarms have the check's name.
count() {
  # shellcheck disable=SC2016 # the backticks are JMESPath literals
  aws cloudwatch describe-alarms --alarm-names "$NAME" \
    --alarm-types MetricAlarm CompositeAlarm \
    --query 'sum([length(MetricAlarms || `[]`), length(CompositeAlarms || `[]`)])' \
    --output text
}

# PutMetricAlarm replaces an alarm of the same name, so never start while one
# exists: it is not this run's to change.
before=$(count) || exit 1
if [ "$before" != 0 ]; then
  echo "::error::An alarm named $NAME already exists; stopping without changing it" >&2
  exit 1
fi

# Deletes the check's alarm if it exists, then confirms it is gone.
cleanup() {
  local n
  n=$(count) || {
    echo "::error::Could not look for $NAME; check for it in CloudWatch and delete it by hand" >&2
    return 1
  }
  [ "$n" = 0 ] && return 0
  aws cloudwatch delete-alarms --alarm-names "$NAME" || true
  n=$(count) || n=unknown
  if [ "$n" != 0 ]; then
    echo "::error::$NAME may still exist; delete it by hand in CloudWatch" >&2
    return 1
  fi
  echo "Deleted $NAME"
}
trap 'cleanup || exit 1' EXIT

# probe LABEL [put-metric-alarm action arguments...] sets RESULT to allowed,
# denied or other. Denied means the identity policy refused this one action;
# a refusal by anything else (a service control policy, say) is other.
RESULT=
probe() {
  local label=$1 out rc
  shift
  out=$(aws cloudwatch put-metric-alarm --alarm-name "$NAME" \
    --alarm-description "Check alarm guard workflow (soccer); safe to delete" \
    --namespace Soccer/AlarmGuardCheck --metric-name NeverPublished \
    --statistic Sum --period 300 --evaluation-periods 1 --threshold 1 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching "$@" 2>&1)
  rc=$?
  echo "$label: exit $rc: ${out:-(no output)}"
  if [ "$rc" = 0 ]; then
    RESULT=allowed
    cleanup || exit 1
  elif printf '%s' "$out" | grep -q 'service control policy\|permissions boundary\|session policy'; then
    RESULT=other
  elif printf '%s' "$out" | grep -q 'not authorized to perform: cloudwatch:PutMetricAlarm'; then
    RESULT=denied
  else
    RESULT=other
  fi
}

probe P --alarm-actions "$ALERTS"
p=$RESULT
probe N --alarm-actions "$DECOY"
n=$RESULT
probe T1 --alarm-actions "$ALERTS" --ok-actions "$DECOY"
t1=$RESULT
probe T2 --alarm-actions "$ALERTS" --insufficient-data-actions "$DECOY"
t2=$RESULT
probe T3 --ok-actions "$DECOY"
t3=$RESULT
echo "P=$p N=$n T1=$t1 T2=$t2 T3=$t3"

if [ "$p" != allowed ] || [ "$n" != denied ]; then
  echo "::error::INCONCLUSIVE: the controls did not hold (P must be allowed and N refused), so the other results mean nothing" >&2
  exit 1
fi
status=0
for result in "OK actions next to an alarm action:$t1" "insufficient-data actions:$t2" "OK actions alone:$t3"; do
  what=${result%:*}
  case ${result##*:} in
    denied) echo "PASS: the guard refuses a decoy in $what" ;;
    allowed)
      echo "::error::FAIL: the guard let a decoy through in $what, so an alarm made by a deploy can take any action there" >&2
      status=1
      ;;
    *)
      echo "::error::INCONCLUSIVE: the call with a decoy in $what was neither made nor refused by the deploy role's policy" >&2
      status=1
      ;;
  esac
done
exit $status
