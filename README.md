# SQL Cost Estimator

Program bira najbolji fizicki plan evaluacije SQL upita i procenjuje cenu u
broju blok transfera.

## Pokretanje

Potreban je Python 3.10 ili noviji. Iz root foldera projekta pokrenuti:

```powershell
python -m pip install -e .
python -m sql_cost_estimator
```

Program podrazumevano cita dva fajla iz root foldera:

- `schema.json` - kompletna sema, statistike, indeksi i broj bafer blokova;
- `query.sql` - SQL upit koji se menja i testira na odbrani.

Za druge fajlove mogu se navesti putanje:

```powershell
python -m sql_cost_estimator --schema druga_schema.json --query drugi_query.sql
```

## Podrzani SQL

- `SELECT` sa eksplicitno navedenim atributima;
- najvise 4 tabele u `FROM`, razdvojene zarezima, sa opcionim aliasima;
- najvise 6 uslova u `WHERE`, povezanih iskljucivo sa `AND`;
- operatori `=`, `!=`, `<>`, `<`, `<=`, `>` i `>=`;
- brojevi i stringovi pod jednostrukim navodnicima kao konstante;
- `ORDER BY` nad najvise jednim atributom, opciono `ASC` ili `DESC`.

Nisu podrzani `JOIN ... ON`, `OR`, podupiti, agregacije, funkcije, `GROUP BY`,
`DISTINCT` i `SELECT *`.

## Izlaz

Operacije najboljeg plana prikazuju se redosledom izvrsavanja. Za svaku
operaciju ispisuju se izabrani algoritam i cena u blok transferima, a na kraju
ukupna cena plana.

## Fajlovi za predaju

```text
sql_cost_estimator/  # izvorni kod i formalna JSON Schema
schema.json          # ulazna sema i statistike
query.sql            # SQL upit
README.md            # uputstvo za pokretanje
pyproject.toml       # instalacija i zavisnosti
```

Folder `DOKUMENTACIJA_ZA_MENE_NE_PREDAVATI` sadrzi detaljna objasnjenja i
dijagrame i nije potreban za pokretanje ili predaju.

Primer arhiviranja, uz zamenu `ggggbbbb` godinom i brojem indeksa:

```powershell
Compress-Archive -Path sql_cost_estimator,schema.json,query.sql,README.md,pyproject.toml -DestinationPath ggggbbbb.zip
```
