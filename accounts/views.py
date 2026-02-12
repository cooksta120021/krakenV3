from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.conf import settings

from vaults.models import ApprovalRequest, AuditLog

from .forms import ApprovalDecisionForm


def waiting_page(request):
    if request.user.is_authenticated:
        if request.user.is_superuser and request.user.role != "admin":
            request.user.role = "admin"
        if request.user.is_superuser or request.user.role == "admin":
            if not request.user.is_approved:
                request.user.is_approved = True
            request.user.save(update_fields=["role", "is_approved"])
            return redirect("/vaults/")
    return render(request, "waiting.html")


def login_redirect(request):
    return render(request, "waiting.html")


def require_role(user, allowed_roles):
    if user.is_superuser:
        return True
    if user.role == "admin" and user.role in allowed_roles:
        return True
    if user.role in allowed_roles and user.is_approved:
        return True
    return False


def role_required(allowed_roles):
    def decorator(view_func):
        def _wrapped(request, *args, **kwargs):
            if not require_role(request.user, allowed_roles):
                return redirect("waiting")
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


@login_required
def approval_queue(request):
    if not require_role(request.user, {"admin", "mod"}):
        return redirect("waiting")

    pending = ApprovalRequest.objects.filter(status="pending").select_related("user", "reviewer")
    form = ApprovalDecisionForm()
    return render(request, "approvals.html", {"pending": pending, "form": form})


@login_required
def approve_user(request):
    if request.method != "POST":
        return redirect("approval_queue")

    if not require_role(request.user, {"admin", "mod"}):
        return redirect("waiting")

    form = ApprovalDecisionForm(request.POST)
    if form.is_valid():
        approval = form.save(commit=False)
        approval.reviewer = request.user
        approval.save()

        user = approval.user
        if approval.status == "approved":
            user.is_approved = True
        user.save()

        # Optional notification if email configured
        if getattr(settings, "DEFAULT_FROM_EMAIL", None) and user.email:
            try:
                send_mail(
                    subject=f"Account {approval.status}",
                    message=f"Your account status is now {approval.status}.",
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    fail_silently=True,
                )
            except Exception:
                pass

        AuditLog.objects.create(
            actor=request.user,
            action=f"approval:{approval.status}",
            target_type="user",
            target_id=str(user.id),
            metadata={"note": approval.note},
        )
    return redirect("approval_queue")
