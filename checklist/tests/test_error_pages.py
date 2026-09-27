"""
Custom 404, 500 and CSRF-failure pages.

With DEBUG=False a visitor otherwise gets Django's bare white pages. What makes
these worth testing is not the copy but the rendering contract, which differs
per handler and is easy to break by making the three files look alike:

    page_not_found  template.render(context, request)   context processors run
    server_error    template.render()                   NO request, NO context
    csrf_failure    t.render(request=request)           processors run, no context dict

500.html is therefore standalone, and `test_500_renders_with_no_context_at_all`
is the guard against a later tidy-up that makes it extend base.html.
"""

# pylint: disable=missing-class-docstring
# pylint: disable=missing-function-docstring

from django.template import Context, Template, engines
from django.template.loader import get_template
from django.test import Client, TestCase, override_settings
from django.urls import path

from checklist.tests.testFactories import SOPFactory


def _boom(request):
    raise RuntimeError("deliberate failure, for the 500 handler")


urlpatterns = [path("boom/", _boom)]


class Test404(TestCase):

    def setUp(self):
        SOPFactory()

    def test_an_unknown_url_uses_the_custom_template(self):
        response = self.client.get("/no-such-waypoint/")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")

    def test_it_renders_the_real_shell(self):
        """
        The point of extending base.html: the pilot keeps the nav. If the
        context processors were not running this would be an empty shell.
        """
        response = self.client.get("/no-such-waypoint/")
        body = response.content.decode()
        self.assertIn("Off the charts", body)
        self.assertIn("conn-brand", body)  # the shell really rendered

    def test_it_offers_a_way_home(self):
        response = self.client.get("/no-such-waypoint/")
        self.assertIn('href="/"', response.content.decode())

    def test_the_requested_path_is_not_echoed_back(self):
        """
        Reflecting attacker-chosen text into a page is a habit worth not having
        even where autoescaping makes it safe, and it tells the pilot nothing.
        """
        response = self.client.get("/no-such-waypoint/CANARY-STRING/")
        self.assertNotIn("CANARY-STRING", response.content.decode())


@override_settings(ROOT_URLCONF=__name__)
class Test500(TestCase):

    def test_a_raising_view_uses_the_custom_template(self):
        client = Client(raise_request_exception=False)
        response = client.get("/boom/")
        self.assertEqual(response.status_code, 500)
        self.assertIn("Unable to comply", response.content.decode())

    def test_it_does_not_leak_the_exception(self):
        client = Client(raise_request_exception=False)
        body = client.get("/boom/").content.decode()
        self.assertNotIn("deliberate failure", body)
        self.assertNotIn("Traceback", body)
        self.assertNotIn("RuntimeError", body)


class Test500IsStandalone(TestCase):
    """
    The guard. server_error() passes no request and no context, so anything
    500.html inherits from base.html would render blank — a conn-bar reading
    "ORIG → DEST", no SOP, a signed-out nav. Django resolves missing variables
    to empty rather than raising, so nothing fails loudly; it just looks broken
    on the one page whose job is to look deliberate.
    """

    def test_500_renders_with_no_context_at_all(self):
        rendered = get_template("500.html").render()

        self.assertIn("Unable to comply", rendered)
        self.assertIn("500", rendered)
        self.assertIn('href="/"', rendered)

    def test_it_does_not_depend_on_the_base_shell(self):
        """
        Structural, not cosmetic: if this ever starts extending base.html the
        assertion below fails before anyone ships a broken-looking 500.
        """
        rendered = get_template("500.html").render()

        for shell_marker in ("conn-brand", "info-panel", "id=\"page\""):
            self.assertNotIn(
                shell_marker,
                rendered,
                f"500.html pulled in {shell_marker!r} from base.html — it renders "
                "with no context processors, so the shell would come out blank",
            )

    def test_its_stylesheets_resolve_without_a_request(self):
        """{% static %} must not need a request, or the 500 page 500s."""
        rendered = get_template("500.html").render()
        self.assertIn("/static/checklist/css/tokens.css", rendered)
        self.assertIn("/static/checklist/css/components.css", rendered)


class TestCsrfFailure(TestCase):

    def setUp(self):
        SOPFactory()

    def test_a_missing_csrf_token_uses_the_custom_template(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post("/login/", {"username": "x", "password": "y"})

        self.assertEqual(response.status_code, 403)
        body = response.content.decode()
        self.assertIn("Clearance expired", body)

    def test_it_replaces_djangos_accusatory_default(self):
        client = Client(enforce_csrf_checks=True)
        body = client.post("/login/", {"username": "x", "password": "y"}).content.decode()

        self.assertNotIn("CSRF verification failed", body)
        self.assertNotIn("Request aborted", body)

    def test_it_points_back_at_sign_in(self):
        client = Client(enforce_csrf_checks=True)
        body = client.post("/login/", {"username": "x", "password": "y"}).content.decode()
        self.assertIn('href="/login/"', body)

    def test_it_does_not_reference_the_unavailable_reason_variables(self):
        """
        csrf_failure calls t.render(request=request) with no context dict, so
        `reason`, `no_referer` and `no_cookie` are NOT available however much
        Django's own source suggests otherwise. Referencing one would render an
        empty string and read as a missing sentence.
        """
        source = (
            engines["django"]
            .engine.get_template("403_csrf.html")
            .source
        )
        for unavailable in ("{{ reason", "{{ no_referer", "{{ no_cookie"):
            self.assertNotIn(unavailable, source)
