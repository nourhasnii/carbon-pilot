from django.core.management.base import BaseCommand

from apps.energy.services.evaluation import evaluate


class Command(BaseCommand):
    help = "Évalue la détection d'anomalies sur des séries synthétiques (précision, rappel, F1, matrice de confusion)."

    def add_arguments(self, parser):
        parser.add_argument("--series", type=int, default=30, help="Nombre de séries simulées (défaut 30).")
        parser.add_argument("--noise", type=float, default=0.03, help="Bruit relatif des mesures (défaut 0,03 = 3 %%).")

    def handle(self, *args, **opts):
        r = evaluate(seeds=range(opts["series"]), noise=opts["noise"])
        w = self.stdout.write
        w(f"Séries simulées : {opts['series']} (bruit {opts['noise']:.0%}), 6 anomalies injectées par série")
        w("")
        w("                    Détecté anormal   Détecté normal")
        w(f"  Vraiment anormal  {r['tp']:>15}   {r['fn']:>14}")
        w(f"  Vraiment normal   {r['fp']:>15}   {r['tn']:>14}")
        w("")
        w(f"Précision : {r['precision']:.1%}   Rappel : {r['recall']:.1%}   F1 : {r['f1']:.1%}")
