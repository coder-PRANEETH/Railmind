from django.urls import path
from . import views

urlpatterns = [
    path('state/', views.get_state, name='get_state'),
    path('reset/', views.reset_twin, name='reset_twin'),
    path('copy/', views.copy_twin, name='copy_twin'),
    path('track/close/', views.close_track, name='close_track'),
    path('weather/set/', views.set_weather, name='set_weather'),
    path('route/find/', views.find_route, name='find_route'),
    path('train/reroute/', views.reroute_train, name='reroute_train'),
    path('action/apply/', views.apply_action, name='apply_action'),
    path('evaluate/delay/', views.calculate_delay, name='calculate_delay'),
    path('evaluate/risk/', views.calculate_risk, name='calculate_risk'),
    path('tick/', views.advance_twin, name='advance_twin'),
    path('workorders/', views.work_orders, name='work_orders'),
    path('workorders/<str:work_order_id>/', views.work_order_detail, name='work_order_detail'),
    path('workorders/<str:work_order_id>/cancel/', views.work_order_cancel, name='work_order_cancel'),
    path('workorders/<str:work_order_id>/retry/', views.work_order_retry, name='work_order_retry'),
]
