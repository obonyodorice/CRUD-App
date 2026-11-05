from django.urls import path
from . import views as v

P = "chama/<int:pk>/"
urlpatterns = [
    path("", v.chama_list, name="chama_list"),
    path("new/", v.chama_create, name="chama_create"),
    path("join/", v.join, name="join_form"),
    path("join/<str:code>/", v.join, name="join"),
    path(P, v.dashboard, name="dashboard"),
    path(P + "members/", v.members, name="members"),
    path(P + "members/<int:mid>/", v.member_detail, name="member_detail"),
    path(P + "members/<int:mid>/update/", v.member_update, name="member_update"),
    path(P + "contributions/", v.contributions, name="contributions"),
    path(P + "contributions/<int:cid>/reverse/", v.contribution_reverse, name="contribution_reverse"),
    path(P + "fines/", v.fines, name="fines"),
    path(P + "fines/<int:fid>/<str:action>/", v.fine_action, name="fine_action"),
    path(P + "loans/", v.loans, name="loans"),
    path(P + "loans/<int:lid>/", v.loan_detail, name="loan_detail"),
    path(P + "meetings/", v.meetings, name="meetings"),
    path(P + "meetings/<int:mid>/", v.meeting_detail, name="meeting_detail"),
    path(P + "expenses/", v.expenses, name="expenses"),
    path(P + "expenses/<int:eid>/<str:action>/", v.expense_action, name="expense_action"),
    path(P + "rotation/", v.rotation, name="rotation"),
    path(P + "rotation/<int:sid>/paid/", v.rotation_paid, name="rotation_paid"),
    path(P + "notices/", v.announcements, name="announcements"),
    path(P + "audit/", v.audit, name="audit"),
    path(P + "add/<slug:kind>/", v.add, name="add"),
]
