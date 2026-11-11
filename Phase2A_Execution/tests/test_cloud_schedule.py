"""Static contracts for the VM power schedule and its operating window."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class CloudScheduleTest(unittest.TestCase):
    def test_power_schedule_uses_ist_weekdays_deallocate_and_scoped_roles(self):
        source = (ROOT / 'deploy/azure/vm-schedule.bicep').read_text()
        for value in ("'India Standard Time'", "'Monday'", "'Friday'", "hours: [7]",
                      "minutes: [30]", "hours: [17]", "/start?api-version=",
                      "/deallocate?api-version=", "scope: vm", "audience: 'https://management.azure.com/'"):
            self.assertIn(value, source)
        self.assertEqual(source.count("principalType: 'ServicePrincipal'"), 2)
        self.assertNotIn('/restart?', source)
        self.assertNotIn('password', source.lower())

    def test_backup_finishes_before_shutdown_and_is_persistent(self):
        timer = (ROOT / 'deploy/systemd/tradex-backup.timer').read_text()
        self.assertIn('OnCalendar=Mon..Fri *-*-* 11:00:00 UTC', timer)
        self.assertIn('Persistent=true', timer)

    def test_monitor_only_runs_inside_powered_window(self):
        workflow = (ROOT / '.github/workflows/cloud-health.yml').read_text()
        crons = re.findall(r"cron: '([^']+)'", workflow)
        self.assertEqual(crons, ['10-55/5 2 * * 1-5', '*/5 3-10 * * 1-5', '0-25/5 11 * * 1-5'])

    def test_upgrade_installs_changed_systemd_timer_before_worker_start(self):
        script = (ROOT / 'deploy/azure/upgrade-upstox-greedy.sh').read_text()
        install = script.index('install -m 644 "$candidate"/deploy/systemd/*')
        reload = script.index('systemctl daemon-reload', install)
        start = script.index('systemctl start tradex.service', reload)
        self.assertLess(install, reload)
        self.assertLess(reload, start)


if __name__ == '__main__':
    unittest.main()
