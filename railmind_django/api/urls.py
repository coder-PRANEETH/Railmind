from django.urls import path
from . import views

urlpatterns = [
    path('state/', views.get_state, name='get_state'),
    path('copy/', views.copy_twin, name='copy_twin'),
    path('track/close/', views.close_track, name='close_track'),
    path('route/find/', views.find_route, name='find_route'),
    path('train/reroute/', views.reroute_train, name='reroute_train'),
    path('action/apply/', views.apply_action, name='apply_action'),
    path('evaluate/delay/', views.calculate_delay, name='calculate_delay'),
    path('evaluate/risk/', views.calculate_risk, name='calculate_risk'),
]
