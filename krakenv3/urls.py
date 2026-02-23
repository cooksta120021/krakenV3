"""
URL configuration for krakenv3 project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework.authtoken import views as drf_authtoken_views

from accounts.views import DashboardView, MeViewSet, SignupView, UserApprovalViewSet, WaitingApprovalView
from api_keys.views import ApiKeyListCreateView, ApiKeyViewSet
from wallets.views import (
    AutoTradeConsoleClearView,
    SleeveViewSet,
    WalletLiveStatusView,
    WalletPageView,
    WalletViewSet,
    TradingHelpPageView,
)
from trading.views import OrderLogViewSet, RateMeterPageView, RateStatusView, SleeveStrategyViewSet, KrakenAssetsView

router = DefaultRouter()
router.register(r'me', MeViewSet, basename='me')
router.register(r'users/approve', UserApprovalViewSet, basename='user-approve')
router.register(r'api-keys', ApiKeyViewSet, basename='api-keys')
router.register(r'wallets', WalletViewSet, basename='wallets')
router.register(r'sleeves', SleeveViewSet, basename='sleeves')
router.register(r'strategies', SleeveStrategyViewSet, basename='strategies')
router.register(r'orders', OrderLogViewSet, basename='orders')

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', DashboardView.as_view(), name='dashboard'),
    path('accounts/', include('django.contrib.auth.urls')),
    path('accounts/signup/', SignupView.as_view(), name='signup'),
    path('accounts/waiting/', WaitingApprovalView.as_view(), name='waiting-approval'),
    path('keys/', ApiKeyListCreateView.as_view(), name='api-keys-page'),
    path('wallets/', WalletPageView.as_view(), name='wallets-page'),
    path('help/trading/', TradingHelpPageView.as_view(), name='trading-help'),
    path('rate-meter/', RateMeterPageView.as_view(), name='rate-meter'),
    path('api/rate/status', RateStatusView.as_view(), name='rate-status'),
    path('api/kraken/assets', KrakenAssetsView.as_view(), name='kraken-assets'),
    path('api/wallets/live', WalletLiveStatusView.as_view(), name='wallets-live'),
    path('api/autotrade/console/clear', AutoTradeConsoleClearView.as_view(), name='autotrade-console-clear'),
    path('api/token/', drf_authtoken_views.obtain_auth_token, name='api-token'),
    path('api/', include(router.urls)),
]
