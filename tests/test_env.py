import tempfile
import unittest
from contextlib import chdir
from pathlib import Path

from tricycle_core import InvalidRequestError, find_dotenv, load_dotenv


class DotenvTests(unittest.TestCase):
    def test_load_dotenv_parses_common_values_and_preserves_shell_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text(
                "# comments are ignored\n"
                "export BOHR_ACCESS_KEY='file-secret'\n"
                'BOHR_MACHINE_TYPE="c64 m128 cpu" # trailing comment\n'
                "BOHR_IMAGE=registry.example/image#tag\n",
                encoding="utf-8",
            )
            environ = {"BOHR_ACCESS_KEY": "shell-secret"}

            self.assertEqual(load_dotenv(path, environ=environ), path.resolve())
            self.assertEqual(environ["BOHR_ACCESS_KEY"], "shell-secret")
            self.assertEqual(environ["BOHR_MACHINE_TYPE"], "c64 m128 cpu")
            self.assertEqual(environ["BOHR_IMAGE"], "registry.example/image#tag")

    def test_load_dotenv_discovers_a_parent_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nested = root / "work" / "nested"
            nested.mkdir(parents=True)
            dotenv = root / ".env"
            dotenv.write_text("BOHR_PROJECT_ID=project-from-file\n", encoding="utf-8")

            environ: dict[str, str] = {}
            with chdir(nested):
                self.assertEqual(find_dotenv(), dotenv.resolve())
                load_dotenv(environ=environ)

            self.assertEqual(environ["BOHR_PROJECT_ID"], "project-from-file")

    def test_restricted_load_does_not_read_secret_into_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text(
                "BOHR_PROJECT_ID=project-from-file\nBOHR_ACCESS_KEY=secret-from-file\n",
                encoding="utf-8",
            )
            environ: dict[str, str] = {}

            load_dotenv(path, environ=environ, keys=("BOHR_PROJECT_ID",))

            self.assertEqual(environ, {"BOHR_PROJECT_ID": "project-from-file"})

    def test_load_dotenv_rejects_malformed_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text("not-an-assignment\n", encoding="utf-8")

            with self.assertRaisesRegex(InvalidRequestError, r"expected KEY=VALUE"):
                load_dotenv(path, environ={})


if __name__ == "__main__":
    unittest.main()
