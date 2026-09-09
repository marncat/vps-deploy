import unittest

from vps_deploy.config import ConfigError
from vps_deploy.ssh_gateway import parse_original_command


class GatewayTests(unittest.TestCase):
    def test_exact_command_is_accepted(self):
        self.assertEqual(
            parse_original_command("deploy example-app 12-2-acde1234abcd"),
            ("example-app", "12-2-acde1234abcd"),
        )

    def test_shell_and_extra_arguments_are_rejected(self):
        for value in (
            "",
            "bash",
            "deploy example-app x;id",
            "deploy example-app ../x",
            "deploy example-app release extra",
            "deploy  example-app release",
            "deploy\nexample-app release",
        ):
            with self.assertRaises(ConfigError, msg=value):
                parse_original_command(value)


if __name__ == "__main__":
    unittest.main()
