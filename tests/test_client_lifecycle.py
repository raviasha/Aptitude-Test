import threading
import unittest


class BrowserLifetimeTests(unittest.TestCase):
    def setUp(self):
        from ksat.client.lifecycle import BrowserLifetime
        self.now = 0.0
        self.life = BrowserLifetime(clock=lambda: self.now)

    def test_startup_and_launch_reservation_expire_at_120_seconds(self):
        self.now = 119.99
        self.assertFalse(self.life.begin_close(maintenance_busy=False))
        self.assertTrue(self.life.reserve_launch())
        self.now = 239.98
        self.assertFalse(self.life.begin_close(maintenance_busy=False))
        self.now = 239.99
        self.assertTrue(self.life.begin_close(maintenance_busy=False))
        self.assertFalse(self.life.begin_close(maintenance_busy=False))
        self.assertFalse(self.life.reserve_launch())
        self.assertFalse(self.life.connect('late'))

    def test_two_tabs_refresh_and_duplicate_disconnect(self):
        self.life.connect('one')
        self.life.connect('two')
        self.now = 500
        self.life.disconnect('one')
        self.assertFalse(self.life.begin_close(maintenance_busy=False))
        self.life.disconnect('two')
        self.now = 514.99
        self.assertFalse(self.life.begin_close(maintenance_busy=False))
        self.life.connect('refresh')
        self.now = 900
        self.assertFalse(self.life.begin_close(maintenance_busy=False))
        self.life.disconnect('refresh')
        self.now = 905
        self.life.disconnect('refresh')
        self.now = 915
        self.assertTrue(self.life.begin_close(maintenance_busy=False))

    def test_first_connection_consumes_startup_grace(self):
        self.life.connect('tab')
        self.life.disconnect('tab')
        self.now = 15
        self.assertTrue(self.life.begin_close(maintenance_busy=False))

    def test_launch_hold_while_existing_tab_closes_is_not_lost(self):
        self.life.connect('old')
        self.now = 10
        self.life.reserve_launch()
        self.life.disconnect('old')
        self.now = 25
        self.assertFalse(self.life.begin_close(maintenance_busy=False))
        self.life.connect('new')
        self.life.disconnect('new')
        self.now = 40
        self.assertTrue(self.life.begin_close(maintenance_busy=False))

    def test_maintenance_and_readiness_do_not_commit_close(self):
        self.now = 121
        self.assertTrue(self.life.ready_to_close())
        self.assertFalse(self.life.begin_close(maintenance_busy=True))
        self.assertFalse(self.life.closing)
        self.assertTrue(self.life.begin_close(maintenance_busy=False))

    def test_connect_and_close_race_has_exactly_one_winner(self):
        self.now = 121
        barrier = threading.Barrier(3)
        results = []
        def run(operation):
            barrier.wait()
            results.append(operation())
        threads = [threading.Thread(target=run, args=(operation,)) for operation in (
            lambda: self.life.connect('race'), lambda: self.life.begin_close(maintenance_busy=False))]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join()
        self.assertEqual([False, True], sorted(results))
