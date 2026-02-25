from django import forms
from django.utils.translation import gettext_lazy as _
from pretix.base.models import Item, Question
from .models import SeatingConfig

Q_SEAT_LABEL = "simpleseating_seat_label"
Q_SEAT_GUID = "simpleseating_seat_guid"

class SeatingSettingsForm(forms.ModelForm):
    svg_file = forms.FileField(required=False, label=_("SVG seating plan"))
    json_file = forms.FileField(required=False, label=_("JSON plan (seats.pretix.eu export)"))
    item = forms.ModelChoiceField(queryset=Item.objects.none(), required=True, label=_("Ticket item"))
    category_variation_map = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows":6, "placeholder":"Category I = 3\nCategory II = 4"}), label=_("Category → variation mapping (one per line, name=id)"))
    class Meta:
        model = SeatingConfig
        fields = ("seat_id_prefix","hold_minutes","category_variation_map")

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        self.fields["item"].queryset = Item.objects.filter(event=event)

    def _ensure_questions(self, cfg, item: Item):
        q_label, _ = Question.objects.get_or_create(
            event=self.event, identifier=Q_SEAT_LABEL,
            defaults={"question":{"en":"Seat","fr":"Siège"}, "type": Question.TYPE_STRING, "required": True, "hidden": False, "ask_during_checkin": False}
        )
        q_guid, _ = Question.objects.get_or_create(
            event=self.event, identifier=Q_SEAT_GUID,
            defaults={"question":{"en":"Seat GUID","fr":"Identifiant siège"}, "type": Question.TYPE_STRING, "required": True, "hidden": True, "ask_during_checkin": False}
        )
        if q_label.ask_during_checkin or q_label.hidden:
            q_label.ask_during_checkin = False
            q_label.hidden = False
            q_label.save(update_fields=["ask_during_checkin","hidden"])
        if q_guid.ask_during_checkin is not False or q_guid.hidden is not True:
            q_guid.ask_during_checkin = False
            q_guid.hidden = True
            q_guid.save(update_fields=["ask_during_checkin","hidden"])
        q_label.items.add(item)
        q_guid.items.add(item)
        cfg.question_label_id = q_label.id
        cfg.question_guid_id = q_guid.id

    def save(self, commit=True):
        cfg = super().save(commit=False)
        item = self.cleaned_data["item"]
        cfg.item_id = item.id
        self._ensure_questions(cfg, item)
        if commit:
            cfg.save()
        return cfg
