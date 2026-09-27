import re
from tempfile import TemporaryDirectory

from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.templatetags.static import static
from django.test import SimpleTestCase, override_settings


@override_settings(
    DEBUG=False,
    ALLOWED_HOSTS=["testserver"],
    WHITENOISE_AUTOREFRESH=False,
    WHITENOISE_USE_FINDERS=False,
)
class ProductionStaticFilesTests(SimpleTestCase):
    """Exercise collected assets through middleware, not runserver's handler."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = cls.enterClassContext(TemporaryDirectory(prefix="rds-static-test-"))
        cls.enterClassContext(override_settings(STATIC_ROOT=root))
        call_command("collectstatic", interactive=False, verbosity=0)

    def test_login_references_a_served_versioned_responsive_stylesheet(self):
        css_url = static("monitor/responsive.css")
        self.assertNotEqual(css_url, "/static/monitor/responsive.css")
        page = self.client.get("/login/")
        self.assertContains(page, css_url)
        response = self.client.get(css_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/css", response["Content-Type"])
        self.assertIn("immutable", response["Cache-Control"])
        content = b"".join(response.streaming_content)
        self.assertIn(b".account-panel", content)
        self.assertIn(b"@media (max-width: 720px)", content)
        response.close()

    def test_django_admin_styles_are_served_without_login(self):
        response = self.client.get(static("admin/css/base.css"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/css", response["Content-Type"])
        response.close()

    def test_gzip_variant_is_available(self):
        response = self.client.get(
            staticfiles_storage.url("monitor/responsive.css"),
            HTTP_ACCEPT_ENCODING="gzip",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Encoding"], "gzip")
        response.close()

    def test_non_static_application_files_are_not_exposed(self):
        for path in ["/static/db.sqlite3", "/static/config/settings.py"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_pwa_manifest_and_service_worker_are_available(self):
        manifest = self.client.get("/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertIn("application/manifest", manifest["Content-Type"])
        self.assertEqual(manifest["Cache-Control"], "no-cache, no-store, must-revalidate")
        self.assertContains(manifest, '"short_name": "HealthGrid"')

        worker = self.client.get("/sw.js")
        self.assertEqual(worker.status_code, 200)
        self.assertIn("application/javascript", worker["Content-Type"])
        self.assertEqual(worker["Service-Worker-Allowed"], "/")
        self.assertEqual(worker["Cache-Control"], "no-cache, no-store, must-revalidate")

    def test_public_metadata_has_explicit_cache_headers(self):
        for path, content_type in [("/robots.txt", "text/plain"), ("/sitemap.xml", "application/xml")]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response["Content-Type"])
                self.assertEqual(response["Cache-Control"], "no-cache, no-store, must-revalidate")

    def test_csp_uses_nonce_for_inline_blocks(self):
        response = self.client.get("/login/")
        csp = response["Content-Security-Policy"]

        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertNotIn("style-src 'self' 'unsafe-inline'", csp)
        self.assertIn("script-src 'self' 'nonce-", csp)
        self.assertIn("style-src 'self' 'nonce-", csp)

        nonce = re.search(r"script-src 'self' 'nonce-([^']+)'", csp).group(1)
        self.assertContains(response, f'<script nonce="{nonce}">')
        self.assertContains(response, f'<style nonce="{nonce}">')
