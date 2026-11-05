import secrets
from decimal import Decimal
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Sum
from django.utils import timezone

ZERO = Decimal("0")
MONEY = dict(max_digits=12, decimal_places=2)


def make_code():
    return secrets.token_urlsafe(6)


def total(qs, field="amount"):
    return qs.aggregate(t=Sum(field))["t"] or ZERO


def wa_link(phone, text):
    p = "".join(c for c in phone if c.isdigit())
    if p.startswith("0"):
        p = "254" + p[1:]
    return f"https://wa.me/{p}?text={quote(text)}"


class User(AbstractUser):
    phone_number = models.CharField(max_length=15, blank=True)


class Chama(models.Model):
    FREQ = [("weekly", "Weekly"), ("monthly", "Monthly")]
    INTEREST = [("flat", "Flat"), ("reducing", "Reducing balance")]
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    contribution_amount = models.DecimalField(**MONEY)
    frequency = models.CharField(max_length=10, choices=FREQ)
    start_date = models.DateField(default=timezone.localdate)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    invite_code = models.CharField(max_length=20, unique=True, default=make_code)
    is_active = models.BooleanField(default=True)
    loan_interest_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("1.5"),
                                             help_text="Percent per month")
    loan_interest_type = models.CharField(max_length=10, choices=INTEREST, default="flat")
    loan_limit_multiple = models.PositiveSmallIntegerField(default=3, help_text="Max loan = this x member's savings")

    def __str__(self):
        return self.name

    def periods_since(self, start):
        start, today = max(start, self.start_date), timezone.localdate()
        if today < start:
            return 0
        if self.frequency == "weekly":
            return (today - start).days // 7 + 1
        return (today.year - start.year) * 12 + today.month - start.month + 1

    def balance(self):
        """Cash the group should hold. Never stored, always computed."""
        income = (total(self.contributions)
                  + total(Fine.objects.filter(membership__chama=self, status="paid"))
                  + total(LoanRepayment.objects.filter(loan__chama=self)))
        out = (total(self.loans.filter(status__in=["active", "cleared", "defaulted"]))
               + total(self.expenses.filter(approval_status="approved"))
               + total(RotationSlot.objects.filter(cycle__chama=self, is_paid_out=True), "payout_amount"))
        return income - out


class Membership(models.Model):
    ROLES = [("chairperson", "Chairperson"), ("secretary", "Secretary"),
             ("treasurer", "Treasurer"), ("member", "Member")]
    STATUS = [("active", "Active"), ("suspended", "Suspended"), ("exited", "Exited")]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="members")
    role = models.CharField(max_length=12, choices=ROLES, default="member")
    status = models.CharField(max_length=10, choices=STATUS, default="active")
    is_admin = models.BooleanField(default=False, help_text="Admins manage the chama's money and records")
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "chama")
        ordering = ["joined_at"]

    @property
    def name(self):
        return self.user.get_full_name() or self.user.username

    def __str__(self):
        return self.name

    def paid(self):
        return total(self.contributions.all())

    def owed(self):
        periods = self.chama.periods_since(timezone.localtime(self.joined_at).date())
        return max(self.chama.contribution_amount * periods - self.paid(), ZERO)

    def fines_due(self):
        return total(self.fines.filter(status="unpaid"))

    def loan_balance(self):
        return sum((l.balance() for l in self.loans.filter(status="active")), ZERO)

    @property
    def reminder_link(self):
        return wa_link(self.user.phone_number,
                       f"Hi {self.name}, a reminder from {self.chama.name}: you owe KES {self.owed():,.0f}. Thank you.")

    @property
    def statement_link(self):
        return wa_link(self.user.phone_number,
                       f"{self.chama.name} statement for {self.name}: saved KES {self.paid():,.0f}, "
                       f"owed KES {self.owed():,.0f}, fines due KES {self.fines_due():,.0f}, "
                       f"loan balance KES {self.loan_balance():,.0f}.")


class Contribution(models.Model):
    METHODS = [("cash", "Cash"), ("mpesa", "M-Pesa")]
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="contributions")
    member = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="contributions")
    amount = models.DecimalField(**MONEY)  # negative amount = reversal entry
    date_paid = models.DateField()
    period = models.CharField(max_length=30, blank=True)
    method = models.CharField(max_length=10, choices=METHODS, default="cash")
    mpesa_code = models.CharField(max_length=20, blank=True)
    note = models.CharField(max_length=200, blank=True)
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversal")
    recorded_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="recorded_contributions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_paid", "-id"]

    def save(self, *args, **kwargs):
        if not self.period:
            self.period = self.date_paid.strftime("%B %Y")
        super().save(*args, **kwargs)

    @property
    def is_reversed(self):
        try:
            return self.reversal is not None
        except Contribution.DoesNotExist:
            return False


class FineType(models.Model):
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="fine_types")
    name = models.CharField(max_length=100)
    default_amount = models.DecimalField(**MONEY)

    def __str__(self):
        return f"{self.name} (KES {self.default_amount:,.0f})"


class Fine(models.Model):
    STATUS = [("unpaid", "Unpaid"), ("paid", "Paid"), ("waived", "Waived")]
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="fines")
    fine_type = models.ForeignKey(FineType, null=True, blank=True, on_delete=models.SET_NULL)
    amount = models.DecimalField(**MONEY)
    reason = models.CharField(max_length=200, blank=True)
    date_issued = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=10, choices=STATUS, default="unpaid")
    issued_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="issued_fines")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_issued", "-id"]


class AuditLog(models.Model):
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="audit_logs")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=50, blank=True)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    details = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]


class Loan(models.Model):
    STATUS = [("requested", "Requested"), ("approved", "Approved"), ("rejected", "Rejected"),
              ("active", "Active"), ("cleared", "Cleared"), ("defaulted", "Defaulted")]
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="loans")
    borrower = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="loans")
    amount = models.DecimalField(**MONEY)
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, help_text="Percent per month")
    interest_type = models.CharField(max_length=10, choices=Chama.INTEREST, default="flat")
    repayment_months = models.PositiveSmallIntegerField(default=3)
    purpose = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=STATUS, default="requested")
    approved_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.PROTECT, related_name="approved_loans")
    requested_at = models.DateTimeField(auto_now_add=True)
    due_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]

    def total_interest(self):
        r, m = self.interest_rate / 100, Decimal(self.repayment_months)
        raw = self.amount * r * m if self.interest_type == "flat" else self.amount * r * (m + 1) / 2
        return raw.quantize(Decimal("0.01"))

    def total_payable(self):
        return self.amount + self.total_interest()

    def installment(self):
        return (self.total_payable() / self.repayment_months).quantize(Decimal("0.01"))

    def amount_paid(self):
        return total(self.repayments)

    def balance(self):
        return self.total_payable() - self.amount_paid()

    @property
    def is_overdue(self):
        return self.status == "active" and self.due_date and self.due_date < timezone.localdate()


class LoanRepayment(models.Model):
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="repayments")
    amount = models.DecimalField(**MONEY)
    date_paid = models.DateField(default=timezone.localdate)
    recorded_by = models.ForeignKey(Membership, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_paid", "-id"]


class Meeting(models.Model):
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="meetings")
    title = models.CharField(max_length=150)
    date = models.DateField()
    venue = models.CharField(max_length=150, blank=True)
    minutes = models.TextField(blank=True)

    class Meta:
        ordering = ["-date"]


class Attendance(models.Model):
    STATUS = [("present", "Present"), ("absent", "Absent"), ("apologies", "Apologies")]
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="attendance")
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=STATUS)

    class Meta:
        unique_together = ("meeting", "membership")


class Expense(models.Model):
    STATUS = [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")]
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="expenses")
    category = models.CharField(max_length=80)
    amount = models.DecimalField(**MONEY)
    description = models.CharField(max_length=200, blank=True)
    receipt = models.ImageField(upload_to="receipts/", blank=True)
    date = models.DateField(default=timezone.localdate)
    recorded_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="recorded_expenses")
    approval_status = models.CharField(max_length=10, choices=STATUS, default="pending")
    approved_by = models.ForeignKey(Membership, null=True, blank=True, on_delete=models.PROTECT, related_name="approved_expenses")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]


class RotationCycle(models.Model):
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="rotations")
    start_date = models.DateField()
    status = models.CharField(max_length=10, default="active")  # active / completed


class RotationSlot(models.Model):
    cycle = models.ForeignKey(RotationCycle, on_delete=models.CASCADE, related_name="slots")
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE)
    position = models.PositiveSmallIntegerField()
    payout_date = models.DateField()
    payout_amount = models.DecimalField(**MONEY)
    is_paid_out = models.BooleanField(default=False)

    class Meta:
        ordering = ["position"]
        unique_together = ("cycle", "membership")


class Announcement(models.Model):
    chama = models.ForeignKey(Chama, on_delete=models.CASCADE, related_name="announcements")
    title = models.CharField(max_length=150)
    message = models.TextField()
    posted_by = models.ForeignKey(Membership, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def share_link(self):
        return wa_link("", f"*{self.title}*\n{self.message}")
