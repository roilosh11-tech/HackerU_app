// Pilot (HackerU, one cohort, full course) — demo seed for the pilot dashboard.
// Live mode ignores `seed` and reads real submissions from GET /api/pilot.
// seed: w = pilot week, s = roster index, nps 0–10, helped = indexes into `helped`, anon = student chose anonymous.
window.PilotData = {
  defaultCourse: 'ai-arch',
  defaultCohort: 'ai-2026a',
  liveStart: '2026-10-19', // live default pilot start (editable on the pilot screen; saved in school settings)
  helped: ['סיכומי השיעורים', 'כרטיסיות חזרה', 'משחקים', 'שאל את הקורס', 'התובנות של המרצה', 'שיעורי בית ומבחנים'],
  seed: [
    { w: 1, s: 1, nps: 8, helped: [0], anon: false, text: 'עוד מתרגלת, אבל הסיכום בראש השיעור כבר חסך לי לחזור על ההקלטה.' },
    { w: 1, s: 4, nps: 6, helped: [], anon: true, text: 'לא הבנתי בהתחלה איפה הכרטיסיות. אולי הדרכה קצרה בכניסה הראשונה?' },
    { w: 1, s: 7, nps: 9, helped: [0, 4], anon: false, text: '' },
    { w: 2, s: 2, nps: 9, helped: [1], anon: false, text: 'הכרטיסיות חוזרות בדיוק כשאני מתחילה לשכוח. זה עובד.' },
    { w: 2, s: 9, nps: 7, helped: [3], anon: false, text: 'שאל את הקורס עונה טוב, אבל לפעמים ארוך מדי.' },
    { w: 3, s: 3, nps: 10, helped: [2, 1], anon: false, text: 'הדו-קרב עם החברים מהכיתה הפך את החזרה למשהו שאני מחכה לו.' },
    { w: 3, s: 11, nps: 5, helped: [0], anon: true, text: 'בטלפון קשה לקרוא את המצגת המוטמעת.' },
    { w: 3, s: 6, nps: 9, helped: [4], anon: false, text: 'התובנות של רילי בסיכום — החלק הכי שימושי.' },
    { w: 4, s: 5, nps: 8, helped: [1, 5], anon: false, text: '' },
    { w: 4, s: 12, nps: 10, helped: [0, 3], anon: true, text: 'פספסתי שיעור בגלל מילואים והשלמתי הכול בערב אחד.' },
    { w: 5, s: 8, nps: 9, helped: [2], anon: false, text: 'ה-Royale ממכר. למדתי מושגים בלי לשים לב.' },
    { w: 5, s: 0, nps: 7, helped: [5], anon: false, text: 'הייתי רוצה תרגיל קטן בסוף כל שיעור כדי לבדוק שהבנתי.' },
    { w: 5, s: 10, nps: 10, helped: [1, 4], anon: false, text: '' },
    { w: 6, s: 13, nps: 9, helped: [3], anon: false, text: 'מרגיש כמו שעת קבלה זמינה כל הזמן.' },
    { w: 6, s: 4, nps: 8, helped: [0, 1], anon: true, text: 'הרבה יותר נוח מאשר בהתחלה. ההתראות על כרטיסיות עוזרות.' },
    { w: 7, s: 2, nps: 10, helped: [1, 2], anon: false, text: 'לפני המבחן עברתי על כל החומר בחצי שעה.' },
    { w: 7, s: 14, nps: 6, helped: [0], anon: false, text: 'חלק מהשיעורים ארוכים — הייתי שמח לחלוקה לפרקים קצרים יותר.' },
    { w: 8, s: 1, nps: 9, helped: [0, 4], anon: false, text: 'אני כבר לא כותבת סיכומים בעצמי.' },
    { w: 8, s: 7, nps: 10, helped: [1, 3], anon: false, text: '' },
  ],
};
