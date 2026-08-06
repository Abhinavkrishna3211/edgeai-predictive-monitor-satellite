#include "hal/hal_provisioning.h"

/*
 * Phase 12a placeholder implementation of hal/hal_provisioning.h — no real
 * AP/portal yet (that's Phase 12b). take_submission() always reports
 * "nothing submitted" so wifi_provision_task.c's PROVISIONING state is
 * reachable and stays stable, but never resolves — exactly what Phase 12a's
 * own scope (state-machine plumbing, not the portal) calls for.
 */

void hal_provisioning_start(void)
{
}

void hal_provisioning_stop(void)
{
}

bool hal_provisioning_active(void)
{
    return false;
}

bool hal_provisioning_take_submission(struct net_credentials *out)
{
    (void)out;
    return false;
}

void hal_provisioning_report_result(bool success)
{
    (void)success;
}
