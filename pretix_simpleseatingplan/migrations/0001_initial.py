from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    initial = True

    dependencies = [("pretixbase", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="SeatingConfig",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("item_id", models.IntegerField(default=0)),
                ("question_label_id", models.IntegerField(default=0)),
                ("question_guid_id", models.IntegerField(default=0)),
                ("svg", models.TextField(blank=True, default="")),
                ("seat_id_prefix", models.CharField(default="seat-", max_length=50)),
                ("hold_minutes", models.PositiveIntegerField(default=10)),
                ("category_variation_map", models.TextField(blank=True, default="")),
                ("event", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="simpleseating_cfg", to="pretixbase.event")),
            ],
        ),
        migrations.CreateModel(
            name="Seat",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("seat_guid", models.CharField(max_length=120)),
                ("label", models.CharField(blank=True, default="", max_length=200)),
                ("category", models.CharField(blank=True, default="", max_length=200)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="simpleseating_seats", to="pretixbase.event")),
            ],
            options={"unique_together": {("event","seat_guid")}},
        ),
        migrations.CreateModel(
            name="SeatHold",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("seat_guid", models.CharField(max_length=120)),
                ("cart_position_id", models.IntegerField()),
                ("expires", models.DateTimeField(db_index=True)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="simpleseating_holds", to="pretixbase.event")),
            ],
            options={"unique_together": {("event","seat_guid")}},
        ),
        migrations.CreateModel(
            name="SeatAssignment",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("seat_guid", models.CharField(max_length=120)),
                ("order_position_id", models.IntegerField(db_index=True)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="simpleseating_assignments", to="pretixbase.event")),
            ],
            options={"unique_together": {("event","seat_guid")}},
        ),
    ]
