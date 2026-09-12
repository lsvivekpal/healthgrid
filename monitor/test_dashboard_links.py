from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import DashboardLink


User = get_user_model()


class DashboardLinkTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user("viewer", password="pw")
        self.admin = User.objects.create_superuser("admin", password="pw")

    def test_authenticated_users_can_view_links_and_links_open_safely(self):
        DashboardLink.objects.create(name="Ops", url="https://ops.example.com", is_active=True)
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("instance-list"))
        self.assertContains(response, "https://ops.example.com")
        self.assertContains(response, 'target="_blank"')
        self.assertContains(response, 'rel="noopener noreferrer"')

    def test_only_administrator_can_manage_links(self):
        self.client.force_login(self.viewer)
        response = self.client.post(reverse("dashboard-links"), {"name": "Ops", "url": "https://ops.example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DashboardLink.objects.exists())

    def test_administrator_can_add_link(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("dashboard-links"), {
            "name": "Ops", "url": "https://ops.example.com", "category": "Monitoring",
            "description": "Operations", "sort_order": "1", "is_active": "1",
        })
        self.assertRedirects(response, reverse("dashboard-links"))
        self.assertTrue(DashboardLink.objects.filter(name="Ops", created_by=self.admin).exists())

    def test_unsafe_dashboard_url_is_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("dashboard-links"), {"name": "Bad", "url": "javascript:alert(1)"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DashboardLink.objects.exists())
