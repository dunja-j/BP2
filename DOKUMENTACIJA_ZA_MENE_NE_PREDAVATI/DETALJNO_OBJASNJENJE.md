# SQL Cost Estimator

Python sistem za parsiranje ogranicenog SQL-a, izbor fizickog plana i procenu
cene u broju disk blok transfera. Biblioteka `jsonschema` proverava zvanicni
format ulaza pre optimizacije.

## Brzi pocetak

Potreban je Python 3.10 ili noviji. Instalacija projekta i zavisnosti:

```powershell
python -m pip install -e .
python -m sql_cost_estimator
```

Program podrazumevano cita kompletnu semu iz [schema.json](../schema.json) i SQL
iz [query.sql](../query.sql). Profesor moze da izmeni `query.sql` i ponovo
pokrene istu komandu.

Alternativni ulazni fajlovi mogu da se navedu eksplicitno:

```powershell
python -m sql_cost_estimator --schema druga_schema.json --query drugi_query.sql
```

Automatizovani testovi se lokalno cuvaju izvan projekta u susednom folderu
`BP2_LOKALNI_TESTOVI`.

## Podrzani SQL

- `SELECT` lista sadrzi samo eksplicitno navedene atribute.
- `FROM` ima najvise 4 tabele razdvojene zarezima, sa opcionim aliasima.
- `WHERE` ima najvise 6 uslova povezanih iskljucivo sa `AND`.
- Operatori uslova su `=`, `!=`, `<>`, `<`, `<=`, `>` i `>=`.
- Konstante u uslovima su brojevi ili stringovi pod jednostrukim navodnicima.
- `ORDER BY` ima najvise jedan atribut i opciono `ASC` ili `DESC`.
- Nisu podrzani `JOIN` sintaksa, podupiti, `OR`, agregacije, funkcije,
  `GROUP BY`, `DISTINCT` i `SELECT *`.

Primer:

```sql
SELECT s.indeks, s.ime, p.naziv, i.ocena, st.iznos
FROM Student s, Ispit i, Predmet p, Stipendija st
WHERE s.indeks = i.studentIndeks
  AND i.predmetId = p.predmetId
  AND s.indeks = st.studentIndeks
  AND s.smer = 'RTI'
  AND i.ocena >= 8
  AND st.tip = 'DRZAVNA'
ORDER BY s.indeks
```

## Ulazni JSON

Kompletan, neizmenjen zadati primer je u [schema.json](../schema.json), a
formalni ugovor u
[input.schema.json](../sql_cost_estimator/input.schema.json).

```json
{
  "bufferBlocks": 10,
  "schema": {
    "tables": [
      {
        "name": "Student",
        "rowCount": 1000,
        "blockCount": 100,
        "rowsPerBlock": 10,
        "attributes": [
          {
            "name": "indeks",
            "type": "STRING",
            "unique": true,
            "distinctValues": 1000
          }
        ],
        "indexes": [
          {
            "name": "idx_student_indeks_clustered",
            "attributes": ["indeks"],
            "type": "B_PLUS_TREE",
            "clustered": true,
            "treeHeight": 3
          }
        ]
      }
    ]
  }
}
```

Ulaz se normalizuje u interne Python modele na sledeci nacin:

- `bufferBlocks` postaje broj bafer blokova;
- `schema.tables` daje katalog tabela;
- `rowCount`, `blockCount` i `rowsPerBlock` opisuju velicinu tabele;
- `distinctValues` postaje statistika $V(A)$ atributa;
- `B_PLUS_TREE` koristi `treeHeight`, a `HASH` nema visinu;
- viseatributski indeksi zadrzavaju redosled atributa iz niza `attributes`.

Zadati format nema SQL polje, zato CLI cita upit iz odvojenog `query.sql`
fajla. Programski API koristi `estimate_file(path, query=sql)`.

Ulaz ne daje sirinu atributa ni velicinu bloka. Sistem zato eksplicitno
pretpostavlja `STRING=32`, `DOUBLE=8`, `INT=4` i `DATE=4` bajta, a korisnu
velicinu bloka procenjuje iz `rowsPerBlock` i sirine redova.

Za B+ stablo je `treeHeight` broj blokova na putu od korena do lista. Polje
`clustered` govori da li su podaci fizicki grupisani po indeksnom kljucu.
Viseatributski indeksi postuju left-prefix pravilo; hash indeks zahteva
jednakost po svim atributima kljuca.

## Fizicki algoritmi

- Selekcija: sekvencijalni prolaz, B+ indeks i hash indeks.
- Slozeni uslovi: guranje lokalnih konjunkcija do bazne tabele i rezidualni
  filter posle indeksnog pristupa.
- Sortiranje: spoljno objedinjeno sortiranje sa vise merge prolaza.
- Spajanje: tuple nested-loop, block nested-loop, index nested-loop,
  sort-merge, one-pass hash i Grace hash join.
- Projekcija: sekvencijalna projekcija bez uklanjanja duplikata.
- Kompletan iskaz: svaki operator materijalizuje izlaz na disk; taj upis je
  ukljucen u cenu.

Optimizer dinamickim programiranjem ispituje sve particije skupa tabela. Za
svaki podskup cuva najjeftiniji neuredjen plan i najjeftinije planove sa
korisnim redosledima. Zbog toga skuplji lokalni plan moze pobediti globalno ako
izbegne kasnije sortiranje.

## Izlaz

Tekstualni izvestaj prikazuje operacije najboljeg plana redosledom izvrsavanja.
Za svaku operaciju prikazuje:

- izabrani algoritam;
- lokalnu cenu u blok transferima.

Na kraju se prikazuje ukupna cena najboljeg plana u blok transferima. Detalji,
formule i alternative ostaju u internom modelu, ali ne opterecuju izlaz za
odbranu.

## Struktura

```text
sql_cost_estimator/
  schema_loader.py   # JSON validacija i statistike seme
  sql_parser.py      # tokenizer, parser i vezivanje atributa
  costing.py         # zajednicke formule selektivnosti i sortiranja
  access_paths.py    # pristupne putanje baznim tabelama
  optimizer.py       # join DP, ORDER BY i projekcija
  reporting.py       # tekstualni izvestaj
  cli.py             # komandna linija
schema.json           # zvanicna sema i statistike
query.sql             # SQL upit koji se menja na odbrani
DOKUMENTACIJA_ZA_MENE_NE_PREDAVATI/
```

Detaljan tok i model troska opisani su u
[ARHITEKTURA.md](ARHITEKTURA.md).

## Pakovanje za predaju

Iz korena projekta u PowerShell-u pokrenuti sledece i zameniti `ggggbbbb`
godinom i brojem indeksa:

```powershell
Compress-Archive -Path sql_cost_estimator,schema.json,query.sql,README.md,pyproject.toml -DestinationPath ggggbbbb.zip
```

Ovako `.git`, `.venv`, kes i lokalni build fajlovi ne ulaze u arhivu.
