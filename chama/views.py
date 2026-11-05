import random
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.forms import modelform_factory
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import RegisterForm
from .models import (Announcement, Attendance, AuditLog, Chama, Contribution, Expense, Fine,
                     FineType, Loan, LoanRepayment, Meeting, Membership, RotationCycle, RotationSlot, wa_link)

ADMIN_NAV = [("dashboard", "Home"), ("members", "Members"), ("contributions", "Contributions"),
             ("fines", "Fines"), ("loans", "Loans"), ("meetings", "Meetings"),
             ("expenses", "Expenses"), ("rotation", "Rotation"), ("announcements", "Notices"),
             ("audit", "Audit")]
MEMBER_NAV = [("dashboard", "Home"), ("contributions", "My contributions"), ("fines", "My fines"),
              ("loans", "My loans"), ("meetings", "Meetings"), ("rotation", "Rotation"),
              ("announcements", "Notices")]


# ---------- helpers ----------
def ctx(request, pk):
    """Return (chama, my membership). 404 if the user is not an active member."""
    chama = get_object_or_404(Chama, pk=pk)
    me = get_object_or_404(Membership, chama=chama, user=request.user, status="active")
    return chama, me


def admin_only(me):
    if not me.is_admin:
        raise PermissionDenied


def page(request, tpl, chama, me, **kw):
    nav = ADMIN_NAV if me.is_admin else MEMBER_NAV
    return render(request, tpl, {"chama": chama, "me": me, "nav": nav, **kw})


def log(chama, request, action, obj=None, details=""):
    AuditLog.objects.create(chama=chama, actor=request.user, action=action, details=details,
                            object_type=type(obj).__name__ if obj else "",
                            object_id=obj.pk if obj else None)


def style(form, chama=None):
    """Date pickers, today's date as default, and only show this chama's records in dropdowns."""
    for f in form.fields.values():
        if isinstance(f, forms.DateField):
            f.widget = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
            f.initial = f.initial or timezone.localdate()
        qs = getattr(f, "queryset", None)
        if chama and qs is not None and hasattr(qs.model, "chama"):
            f.queryset = qs.filter(chama=chama, status="active") if qs.model is Membership else qs.filter(chama=chama)


# ---------- accounts, chamas, joining ----------
def register(request):
    form = RegisterForm(request.POST or None)
    if form.is_valid():
        login(request, form.save())
        return redirect("chama_list")
    return render(request, "registration/register.html", {"form": form})


@login_required
def chama_list(request):
    ms = Membership.objects.filter(user=request.user, status="active").select_related("chama")
    return render(request, "chama_list.html", {"memberships": ms})


@login_required
def chama_create(request):
    Form = modelform_factory(Chama, fields=["name", "description", "contribution_amount", "frequency",
                                            "start_date", "loan_interest_rate", "loan_interest_type",
                                            "loan_limit_multiple"])
    form = Form(request.POST or None)
    style(form)
    if form.is_valid():
        c = form.save(commit=False)
        c.created_by = request.user
        c.save()
        Membership.objects.create(chama=c, user=request.user, role="treasurer", is_admin=True)
        log(c, request, "Created chama", c)
        return redirect("dashboard", pk=c.pk)
    return render(request, "form.html", {"form": form, "title": "Create a chama", "back": "/"})


@login_required
def join(request, code=""):
    code = (request.POST.get("code") or code).strip()
    target = Chama.objects.filter(invite_code=code, is_active=True).first() if code else None
    if request.method == "POST" and target and request.POST.get("confirm"):
        m, created = Membership.objects.get_or_create(chama=target, user=request.user)
        if created:
            log(target, request, "Joined chama", m)
        messages.success(request, f"Welcome to {target.name}!")
        return redirect("dashboard", pk=target.pk)
    if code and not target:
        messages.error(request, "Invalid invite code.")
    return render(request, "join.html", {"target": target, "code": code})


# ---------- dashboards (admin and member see different pages) ----------
@login_required
def dashboard(request, pk):
    chama, me = ctx(request, pk)
    today = timezone.localdate()
    common = dict(balance=chama.balance(), announcements=chama.announcements.all()[:3],
                  next_meeting=chama.meetings.filter(date__gte=today).order_by("date").first())
    if me.is_admin:
        rows = [{"m": m, "paid": m.paid(), "owed": m.owed(), "fines": m.fines_due(), "loan": m.loan_balance()}
                for m in chama.members.filter(status="active").select_related("user")]
        return page(request, "dashboard_admin.html", chama, me, rows=rows,
                    collected=sum(r["paid"] for r in rows),
                    behind=sum(1 for r in rows if r["owed"] > 0),
                    pending_loans=chama.loans.filter(status="requested").count(),
                    pending_expenses=chama.expenses.filter(approval_status="pending").count(),
                    overdue=chama.loans.filter(status="active", due_date__lt=today), **common)
    return page(request, "dashboard_member.html", chama, me, paid=me.paid(), owed=me.owed(),
                fines_due=me.fines_due(), loans=me.loans.filter(status="active"),
                recent=me.contributions.all()[:5], **common)


# ---------- members ----------
@login_required
def members(request, pk):
    chama, me = ctx(request, pk)
    admin_only(me)
    invite = request.build_absolute_uri(reverse("join", args=[chama.invite_code]))
    invite_wa = None
    if request.method == "POST" and request.POST.get("phone"):
        invite_wa = wa_link(request.POST["phone"],
                            f"You're invited to join {chama.name}. Register or log in, then open: "
                            f"{invite} (invite code: {chama.invite_code})")
    return page(request, "members.html", chama, me, members=chama.members.select_related("user"),
                roles=Membership.ROLES, statuses=Membership.STATUS, invite=invite, invite_wa=invite_wa)


@login_required
def member_update(request, pk, mid):
    chama, me = ctx(request, pk)
    if me.role != "treasurer":
        raise PermissionDenied
    m = get_object_or_404(Membership, pk=mid, chama=chama)
    if request.method == "POST" and m != me:
        role, status = request.POST.get("role"), request.POST.get("status")
        if role in dict(Membership.ROLES) and status in dict(Membership.STATUS):
            m.role, m.status = role, status
            m.is_admin = bool(request.POST.get("is_admin")) or role == "treasurer"
            m.save()
            log(chama, request, "Updated member", m, f"{role}, {status}, admin={m.is_admin}")
            messages.success(request, f"{m.name} updated.")
    return redirect("members", pk=pk)


@login_required
def member_detail(request, pk, mid):
    chama, me = ctx(request, pk)
    m = get_object_or_404(Membership, pk=mid, chama=chama)
    if m != me and not me.is_admin:
        raise PermissionDenied
    return page(request, "member_detail.html", chama, me, m=m,
                contributions=m.contributions.select_related("reversal"),
                fines=m.fines.all(), loans=m.loans.all())


# ---------- contributions ----------
@login_required
def contributions(request, pk):
    chama, me = ctx(request, pk)
    items = chama.contributions.select_related("member__user", "recorded_by__user", "reversal")
    if not me.is_admin:
        items = items.filter(member=me)
    return page(request, "contributions.html", chama, me, items=items)


@login_required
def contribution_reverse(request, pk, cid):
    chama, me = ctx(request, pk)
    admin_only(me)
    c = get_object_or_404(Contribution, pk=cid, chama=chama)
    if request.method == "POST" and c.amount > 0 and not c.is_reversed:
        Contribution.objects.create(
            chama=chama, member=c.member, amount=-c.amount, date_paid=timezone.localdate(),
            method=c.method, reverses=c, recorded_by=me,
            note=request.POST.get("reason", "").strip() or "Reversal")
        log(chama, request, "Reversed contribution", c, request.POST.get("reason", ""))
        messages.success(request, "Entry reversed. A correcting entry was added.")
    return redirect("contributions", pk=pk)


# ---------- fines ----------
@login_required
def fines(request, pk):
    chama, me = ctx(request, pk)
    qs = Fine.objects.filter(membership__chama=chama).select_related("membership__user", "fine_type")
    if not me.is_admin:
        qs = qs.filter(membership=me)
    return page(request, "fines.html", chama, me, fines=qs, types=chama.fine_types.all())


@login_required
def fine_action(request, pk, fid, action):
    chama, me = ctx(request, pk)
    admin_only(me)
    f = get_object_or_404(Fine, pk=fid, membership__chama=chama)
    if request.method == "POST" and f.status == "unpaid" and action in ("paid", "waived"):
        f.status = action
        f.save()
        log(chama, request, f"Fine {action}", f)
    return redirect("fines", pk=pk)


# ---------- loans ----------
@login_required
def loans(request, pk):
    chama, me = ctx(request, pk)
    qs = chama.loans.select_related("borrower__user")
    if not me.is_admin:
        qs = qs.filter(borrower=me)
    return page(request, "loans.html", chama, me, loans=qs)


@login_required
def loan_detail(request, pk, lid):
    chama, me = ctx(request, pk)
    loan = get_object_or_404(Loan, pk=lid, chama=chama)
    if loan.borrower != me and not me.is_admin:
        raise PermissionDenied
    if request.method == "POST":
        admin_only(me)
        action = request.POST.get("action")
        if action in ("approve", "reject") and loan.borrower == me:
            messages.error(request, "You cannot decide on your own loan. Ask another admin.")
        elif action == "approve" and loan.status == "requested":
            if chama.balance() < loan.amount:
                messages.error(request, "The group balance is too low to fund this loan.")
            else:
                loan.status, loan.approved_by = "active", me
                loan.due_date = timezone.localdate() + timedelta(days=30 * loan.repayment_months)
                loan.save()
                log(chama, request, "Approved loan", loan, f"KES {loan.amount}")
        elif action == "reject" and loan.status == "requested":
            loan.status, loan.approved_by = "rejected", me
            loan.save()
            log(chama, request, "Rejected loan", loan)
        elif action == "default" and loan.status == "active":
            loan.status = "defaulted"
            loan.save()
            log(chama, request, "Marked loan defaulted", loan)
        elif action == "repay" and loan.status == "active":
            try:
                amt = Decimal(request.POST["amount"])
            except (InvalidOperation, KeyError):
                amt = Decimal(0)
            if amt <= 0 or amt > loan.balance():
                messages.error(request, "Enter an amount between 1 and the loan balance.")
            else:
                LoanRepayment.objects.create(loan=loan, amount=amt, recorded_by=me)
                if loan.balance() <= 0:
                    loan.status = "cleared"
                    loan.save()
                log(chama, request, "Loan repayment", loan, f"KES {amt}")
        return redirect("loan_detail", pk=pk, lid=lid)
    return page(request, "loan_detail.html", chama, me, loan=loan, repayments=loan.repayments.all())


# ---------- meetings ----------
@login_required
def meetings(request, pk):
    chama, me = ctx(request, pk)
    return page(request, "meetings.html", chama, me, meetings=chama.meetings.all())


@login_required
def meeting_detail(request, pk, mid):
    chama, me = ctx(request, pk)
    meeting = get_object_or_404(Meeting, pk=mid, chama=chama)
    people = chama.members.filter(status="active").select_related("user")
    if request.method == "POST":
        admin_only(me)
        meeting.minutes = request.POST.get("minutes", "")
        meeting.save()
        for m in people:
            s = request.POST.get(f"att_{m.pk}")
            if s in dict(Attendance.STATUS):
                Attendance.objects.update_or_create(meeting=meeting, membership=m, defaults={"status": s})
        log(chama, request, "Updated meeting", meeting)
        messages.success(request, "Minutes and attendance saved.")
        return redirect("meeting_detail", pk=pk, mid=mid)
    att = {a.membership_id: a.status for a in meeting.attendance.all()}
    return page(request, "meeting_detail.html", chama, me, meeting=meeting,
                rows=[(m, att.get(m.pk, "")) for m in people], statuses=Attendance.STATUS)


# ---------- expenses ----------
@login_required
def expenses(request, pk):
    chama, me = ctx(request, pk)
    admin_only(me)
    return page(request, "expenses.html", chama, me,
                expenses=chama.expenses.select_related("recorded_by__user"))


@login_required
def expense_action(request, pk, eid, action):
    chama, me = ctx(request, pk)
    admin_only(me)
    e = get_object_or_404(Expense, pk=eid, chama=chama)
    if request.method == "POST" and e.approval_status == "pending" and action in ("approve", "reject"):
        other_admins = chama.members.filter(is_admin=True, status="active").exclude(pk=me.pk).exists()
        if e.recorded_by == me and other_admins:
            messages.error(request, "Another admin must approve an expense you recorded.")
        elif action == "approve" and chama.balance() < e.amount:
            messages.error(request, "The group balance is too low for this expense.")
        else:
            e.approval_status = "approved" if action == "approve" else "rejected"
            e.approved_by = me
            e.save()
            log(chama, request, f"Expense {e.approval_status}", e, f"KES {e.amount}")
    return redirect("expenses", pk=pk)


# ---------- rotation (merry-go-round) ----------
@login_required
def rotation(request, pk):
    chama, me = ctx(request, pk)
    if request.method == "POST":
        admin_only(me)
        try:
            start = date.fromisoformat(request.POST["start_date"])
            amt = Decimal(request.POST["payout_amount"])
        except (ValueError, KeyError, InvalidOperation):
            messages.error(request, "Enter a valid start date and payout amount.")
            return redirect("rotation", pk=pk)
        chama.rotations.filter(status="active").update(status="completed")
        cycle = RotationCycle.objects.create(chama=chama, start_date=start)
        people = list(chama.members.filter(status="active"))
        if request.POST.get("order") == "random":
            random.shuffle(people)
        step = 7 if chama.frequency == "weekly" else 30
        for i, m in enumerate(people):
            RotationSlot.objects.create(cycle=cycle, membership=m, position=i + 1,
                                        payout_date=start + timedelta(days=step * i), payout_amount=amt)
        log(chama, request, "Created rotation cycle", cycle)
        return redirect("rotation", pk=pk)
    cycle = chama.rotations.filter(status="active").last()
    return page(request, "rotation.html", chama, me, cycle=cycle,
                slots=cycle.slots.select_related("membership__user") if cycle else [],
                default_amount=chama.contribution_amount * chama.members.filter(status="active").count())


@login_required
def rotation_paid(request, pk, sid):
    chama, me = ctx(request, pk)
    admin_only(me)
    slot = get_object_or_404(RotationSlot, pk=sid, cycle__chama=chama)
    if request.method == "POST" and not slot.is_paid_out:
        slot.is_paid_out = True
        slot.save()
        if not slot.cycle.slots.filter(is_paid_out=False).exists():
            slot.cycle.status = "completed"
            slot.cycle.save()
        log(chama, request, "Rotation payout", slot, f"KES {slot.payout_amount} to {slot.membership}")
    return redirect("rotation", pk=pk)


# ---------- notices and audit ----------
@login_required
def announcements(request, pk):
    chama, me = ctx(request, pk)
    return page(request, "announcements.html", chama, me, items=chama.announcements.all())


@login_required
def audit(request, pk):
    chama, me = ctx(request, pk)
    admin_only(me)
    return page(request, "audit.html", chama, me, logs=chama.audit_logs.select_related("actor")[:300])


# ---------- one generic "add" view for simple records ----------
def check_contribution(o, chama, me):
    o.mpesa_code = o.mpesa_code.strip().upper()
    if o.method == "mpesa" and not o.mpesa_code:
        return "M-Pesa code is required for M-Pesa payments."
    if o.mpesa_code and Contribution.objects.filter(chama=chama, mpesa_code=o.mpesa_code,
                                                    reversal__isnull=True).exists():
        return "That M-Pesa code was already recorded."


def prep_loan(o, chama, me):
    o.borrower, o.interest_rate, o.interest_type = me, chama.loan_interest_rate, chama.loan_interest_type


def check_loan(o, chama, me):
    if Loan.objects.filter(borrower=me, status__in=["requested", "active"]).exists():
        return "You already have a pending or active loan."
    limit = me.paid() * chama.loan_limit_multiple
    if o.amount > limit:
        return f"Your loan limit is KES {limit:,.0f} ({chama.loan_limit_multiple}x your savings)."


ADD = {
    "contribution": dict(model=Contribution, fields=["member", "amount", "date_paid", "method", "mpesa_code"],
                         title="Record contribution", back="contributions", admin=True,
                         by="recorded_by", check=check_contribution),
    "fine_type": dict(model=FineType, fields=["name", "default_amount"], title="New fine type",
                      back="fines", admin=True),
    "fine": dict(model=Fine, fields=["membership", "fine_type", "amount", "reason"], title="Issue a fine",
                 back="fines", admin=True, by="issued_by"),
    "meeting": dict(model=Meeting, fields=["title", "date", "venue"], title="Schedule a meeting",
                    back="meetings", admin=True),
    "expense": dict(model=Expense, fields=["category", "amount", "description", "receipt", "date"],
                    title="Record an expense", back="expenses", admin=True, by="recorded_by"),
    "announcement": dict(model=Announcement, fields=["title", "message"], title="New announcement",
                         back="announcements", admin=True, by="posted_by"),
    "loan": dict(model=Loan, fields=["amount", "repayment_months", "purpose"], title="Request a loan",
                 back="loans", prep=prep_loan, check=check_loan),
}


@login_required
def add(request, pk, kind):
    chama, me = ctx(request, pk)
    cfg = ADD.get(kind)
    if not cfg:
        raise Http404
    if cfg.get("admin"):
        admin_only(me)
    form = modelform_factory(cfg["model"], fields=cfg["fields"])(request.POST or None, request.FILES or None)
    style(form, chama)
    back = reverse(cfg["back"], args=[pk])
    if form.is_valid():
        obj = form.save(commit=False)
        if hasattr(obj, "chama_id"):
            obj.chama = chama
        if cfg.get("by"):
            setattr(obj, cfg["by"], me)
        if cfg.get("prep"):
            cfg["prep"](obj, chama, me)
        err = None
        if getattr(obj, "amount", 1) <= 0:
            err = "Amount must be greater than zero."
        elif cfg.get("check"):
            err = cfg["check"](obj, chama, me)
        if err:
            form.add_error(None, err)
        else:
            obj.save()
            log(chama, request, f"Added {kind.replace('_', ' ')}", obj)
            messages.success(request, "Saved.")
            return redirect(back)
    return page(request, "form.html", chama, me, form=form, title=cfg["title"], back=back)
