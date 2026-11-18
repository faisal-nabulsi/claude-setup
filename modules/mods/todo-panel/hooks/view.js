// Pure layout for the /todo panel, kept apart so tests can check it.
// Input: today.py --json  →  { now, missed: [item], days: [{ label, items: [item] }], later }
// item: { time, title, tag, soft, urgent }

export function viewRows(data) {
  const rows = [{ kind: 'header', text: data.now }]
  if ((data.missed || []).length > 0) {
    rows.push({ kind: 'section', text: 'Missed? Not marked done', missed: true })
    for (const it of data.missed) rows.push({ kind: 'item', ...it, missed: true })
  }
  for (const day of data.days || []) {
    rows.push({ kind: 'section', text: day.label.toUpperCase() })
    for (const it of day.items) rows.push({ kind: 'item', ...it })
  }
  if ((data.days || []).length === 0) rows.push({ kind: 'section', text: 'Nothing in the next 7 days' })
  const later = data.later ? `+${data.later} more the week after · ` : ''
  rows.push({ kind: 'footer', text: later + 'dim = self-imposed · /todo text for plain text · Esc to close' })
  return rows
}
