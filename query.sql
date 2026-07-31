SELECT s.indeks, s.ime, p.naziv, i.ocena, st.iznos
FROM Student s, Ispit i, Predmet p, Stipendija st
WHERE s.indeks = i.studentIndeks
  AND i.predmetId = p.predmetId
  AND s.indeks = st.studentIndeks
  AND s.smer = 'RTI'
  AND i.ocena >= 8
  AND st.tip = 'DRZAVNA'
ORDER BY s.indeks;