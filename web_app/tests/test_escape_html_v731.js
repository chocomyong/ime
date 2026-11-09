// [v6.7.731] 결함 1 검증 - common.js escapeHtml 가 따옴표(" ')를 이스케이프하는지.
// 실제 common.js 소스에서 escapeHtml 선언을 추출해 그대로 평가한다(사본 테스트 금지).
// 뮤테이션: 종전 DOM 기반(textContent->innerHTML) 구현은 따옴표를 남기므로 아래 attr 단언이 FAIL 한다.
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'ui', 'common.js'), 'utf8');
const m = src.match(/^function escapeHtml\(.*\}$/m);
if (!m) { console.error('escapeHtml 선언을 찾지 못함'); process.exit(2); }
// 추출한 선언을 함수식으로 평가 (document 미사용 구현이어야 한다 - 참조하면 ReferenceError 로 드러남)
const escapeHtml = eval('(' + m[0].replace(/^function escapeHtml/, 'function') + ')');

const fails = [];
function eq(actual, expected, label) {
  if (actual !== expected) fails.push(`${label}: got ${JSON.stringify(actual)} want ${JSON.stringify(expected)}`);
}
// 핵심 결함 수정: 따옴표 이스케이프 (속성값 주입 차단)
eq(escapeHtml('a"b'), 'a&quot;b', 'double-quote');
eq(escapeHtml("a'b"), 'a&#39;b', 'single-quote');
// 속성 탈출 시나리오 - " 로 title 속성을 깨고 onmouseover 주입하는 입력
eq(escapeHtml('" onmouseover="alert(1)'), '&quot; onmouseover=&quot;alert(1)', 'attr-escape');
// 기존 &<> 보존 + 이중 이스케이프 없음
eq(escapeHtml('<b>&'), '&lt;b&gt;&amp;', 'angle-amp');
eq(escapeHtml('a&amp;b'), 'a&amp;amp;b', 'no-double-unescape');  // & 를 먼저 치환하므로 &amp; 가 &amp;amp;
eq(escapeHtml(''), '', 'empty');
eq(escapeHtml(null), '', 'non-string');
eq(escapeHtml('plain'), 'plain', 'plain');

// 소스가 DOM API(document/createElement)를 쓰지 않는지(속성 안전 치환 방식 확인)
if (/document|createElement/.test(m[0])) fails.push('escapeHtml 가 아직 DOM 기반이다 - 따옴표 미이스케이프 위험');

// highlightKeywords: 원문 매칭 + 조각 이스케이프. 키워드가 엔티티 이름과 겹쳐도('quot'·'39'·'amp') 엔티티를 깨지 않는다.
const hm = src.match(/^function highlightKeywords\([\s\S]*?\n\}\n/m);
if (!hm) { console.error('highlightKeywords 선언을 찾지 못함'); process.exit(2); }
const highlightKeywords = eval('(' + hm[0].replace(/^function highlightKeywords/, 'function') + ')');
const MK = t => `<mark class="kw-highlight">${t}</mark>`;
eq(highlightKeywords('say "39" & amp', '39,amp,quot', false), `say &quot;${MK('39')}&quot; &amp; ${MK('amp')}`, 'hl-entity-safe');
eq(highlightKeywords('mark class', 'mark,class', false), `${MK('mark')} ${MK('class')}`, 'hl-no-tag-rematch');
eq(highlightKeywords('<b>', null, false), '&lt;b&gt;', 'hl-no-keywords');
eq(highlightKeywords('Abc', 'abc', true), '<mark class="kw-highlight kw-highlight-super">Abc</mark>', 'hl-super-case');

if (fails.length) { console.log('[FAIL] escapeHtml'); fails.forEach(f => console.log('  -', f)); process.exit(1); }
console.log('[PASS] escapeHtml 따옴표 이스케이프 (' + JSON.stringify(escapeHtml('x"\'<>&')) + ')');
