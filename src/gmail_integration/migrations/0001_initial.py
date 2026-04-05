import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("invoices", "0004_duplicate_dismissed"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="GmailIntegration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("gmail_address", models.EmailField(max_length=254)),
                ("access_token",  models.TextField()),
                ("refresh_token", models.TextField()),
                ("token_expiry",  models.DateTimeField(blank=True, null=True)),
                ("is_active",     models.BooleanField(default=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("created_at",    models.DateTimeField(auto_now_add=True)),
                ("updated_at",    models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="gmail_integration",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="GmailSyncedMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("message_id",          models.CharField(max_length=255)),
                ("attachment_id",       models.CharField(max_length=255)),
                ("subject",             models.CharField(blank=True, max_length=500)),
                ("sender",              models.CharField(blank=True, max_length=500)),
                ("received_at",         models.DateTimeField(blank=True, null=True)),
                ("attachment_filename", models.CharField(blank=True, max_length=255)),
                ("invoice_detected",    models.BooleanField(default=False)),
                ("synced_at",           models.DateTimeField(auto_now_add=True)),
                (
                    "integration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="synced_messages",
                        to="gmail_integration.gmailintegration",
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="gmail_source",
                        to="invoices.invoice",
                    ),
                ),
            ],
            options={
                "ordering": ["-synced_at"],
            },
        ),
        migrations.AlterUniqueTogether(
            name="gmailsyncedmessage",
            unique_together={("integration", "message_id", "attachment_id")},
        ),
    ]
