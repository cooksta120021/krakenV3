from django import forms

from vaults.models import ApprovalRequest


class ApprovalDecisionForm(forms.ModelForm):
    class Meta:
        model = ApprovalRequest
        fields = ["user", "status", "note"]
