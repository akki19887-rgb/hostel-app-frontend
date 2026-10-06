// Tailwind ko sirf index.html padhna hai. Wahi se class dhoondh kar CSS banti hai.
//
// Ye file deploy ke waqt GitHub Actions par chalti hai (firebase-deploy.yml),
// aapke computer par kuch install karne ki zaroorat nahi.
module.exports = {
  content: ['./index.html'],
  theme: { extend: {} },
};
