from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("positions/", views.positions, name="positions"),
    path("wallets/", views.wallets, name="wallets"),
    path("wallets/<int:wallet_id>/delete/", views.delete_wallet, name="delete_wallet"),
    path("vaults/", views.vault_list, name="vault_list"),
    path("vaults/<int:vault_id>/delete/", views.delete_vault, name="delete_vault"),
    path("vaults/<int:vault_id>/pause/", views.pause_vault, name="pause_vault"),
    path("vaults/<int:vault_id>/resume/", views.resume_vault, name="resume_vault"),
    path("vaults/<int:vault_id>/start/", views.start_bot, name="start_bot"),
    path("vaults/<int:vault_id>/stop/", views.stop_bot, name="stop_bot"),
    path("api-keys/", views.api_keys, name="api_keys"),
    path("api-keys/<int:api_key_id>/delete/", views.delete_api_key, name="delete_api_key"),
    path("api-keys/<int:api_key_id>/validate/", views.validate_api_key, name="validate_api_key"),
    path("api-keys/rotate/", views.rotate_api_key, name="rotate_api_key"),
    path("admin/", views.admin_dashboard, name="admin_dashboard"),
    path("toggle-mode/", views.toggle_paper_trading, name="toggle_mode"),
]
