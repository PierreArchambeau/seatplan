from django.db import models
from django.utils import timezone
from pretix.base.models import Event

class SeatingConfig(models.Model):
    event = models.OneToOneField(Event, on_delete=models.CASCADE, related_name="simpleseating_cfg")
    item_id = models.IntegerField(default=0)
    question_label_id = models.IntegerField(default=0)
    question_guid_id = models.IntegerField(default=0)
    svg = models.TextField(blank=True, default="")
    seat_id_prefix = models.CharField(max_length=50, default="seat-")
    hold_minutes = models.PositiveIntegerField(default=10)
    category_variation_map = models.TextField(blank=True, default="")

class Seat(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="simpleseating_seats")
    seat_guid = models.CharField(max_length=120)
    label = models.CharField(max_length=200, blank=True, default="")
    category = models.CharField(max_length=200, blank=True, default="")
    class Meta:
        unique_together = (("event","seat_guid"),)

class SeatHold(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="simpleseating_holds")
    seat_guid = models.CharField(max_length=120)
    cart_position_id = models.IntegerField()
    expires = models.DateTimeField(db_index=True)
    class Meta:
        unique_together = (("event","seat_guid"),)
    @property
    def is_expired(self):
        return self.expires <= timezone.now()

class SeatAssignment(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="simpleseating_assignments")
    seat_guid = models.CharField(max_length=120)
    order_position_id = models.IntegerField(db_index=True)
    class Meta:
        unique_together = (("event","seat_guid"),)
