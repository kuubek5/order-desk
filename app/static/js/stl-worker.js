/* Фоновий потік STL-прев'ю (власник 28.10.26: «розгортається ривками, як
 * слайд-шоу»). Замір у Edge з уповільненим у 4× процесором: прийом тіла файлу
 * (14 МБ — ~170 мс) ішов головним потоком ПІД анімацію розкриття панелі, а
 * розбір, нормалі й центрування — ще ~570 мс одразу після неї. Тут усе це
 * поза головним потоком: сторінка лише отримує готові масиви (transfer, без
 * копіювання) і кладе їх у BufferGeometry.
 *
 * Лише бінарний STL (так пише CAD). Текстовий повертається як є — його
 * розбирає STLLoader на головному потоці, як і раніше.
 *
 * Нормалі — ті самі, що дає three computeVertexNormals() для неіндексованої
 * геометрії: (C − B) × (A − B), нормовані, однакові на три вершини грані.
 * Нормалі з файлу не беремо — CAD часто пише нулі (див. stl-render-core.js).
 */
"use strict";

self.onmessage = function (event) {
  var id = event.data.id;
  fetch(event.data.url)
    .then(function (response) {
      if (!response.ok) throw new Error("file-failed");
      return response.arrayBuffer();
    })
    .then(function (buffer) {
      var parsed = parseBinary(buffer);
      if (!parsed) {
        self.postMessage({ id: id, raw: buffer }, [buffer]);
        return;
      }
      self.postMessage(
        { id: id, positions: parsed.positions, normals: parsed.normals, size: parsed.size },
        [parsed.positions.buffer, parsed.normals.buffer]
      );
    })
    .catch(function (err) {
      self.postMessage({ id: id, error: String((err && err.message) || err) });
    });
};

function parseBinary(buffer) {
  if (buffer.byteLength < 84) return null;
  var view = new DataView(buffer);
  var faces = view.getUint32(80, true);
  if (84 + faces * 50 !== buffer.byteLength) return null;

  var positions = new Float32Array(faces * 9);
  var normals = new Float32Array(faces * 9);
  var minX = Infinity, minY = Infinity, minZ = Infinity;
  var maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;

  for (var f = 0; f < faces; f++) {
    var src = 84 + f * 50 + 12; // пропускаємо нормаль грані з файлу
    var dst = f * 9;
    for (var k = 0; k < 9; k++) {
      var v = view.getFloat32(src + k * 4, true);
      positions[dst + k] = v;
    }
    for (var p = 0; p < 9; p += 3) {
      var x = positions[dst + p], y = positions[dst + p + 1], z = positions[dst + p + 2];
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
      if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
    }
    // (C − B) × (A − B)
    var ax = positions[dst], ay = positions[dst + 1], az = positions[dst + 2];
    var bx = positions[dst + 3], by = positions[dst + 4], bz = positions[dst + 5];
    var cx = positions[dst + 6], cy = positions[dst + 7], cz = positions[dst + 8];
    var ux = cx - bx, uy = cy - by, uz = cz - bz;
    var wx = ax - bx, wy = ay - by, wz = az - bz;
    var nx = uy * wz - uz * wy;
    var ny = uz * wx - ux * wz;
    var nz = ux * wy - uy * wx;
    var len = Math.sqrt(nx * nx + ny * ny + nz * nz) || 1;
    nx /= len; ny /= len; nz /= len;
    for (var q = 0; q < 9; q += 3) {
      normals[dst + q] = nx;
      normals[dst + q + 1] = ny;
      normals[dst + q + 2] = nz;
    }
  }

  // Центрування — як geometry.center(): середина рамки в нуль.
  var mx = faces ? (minX + maxX) / 2 : 0;
  var my = faces ? (minY + maxY) / 2 : 0;
  var mz = faces ? (minZ + maxZ) / 2 : 0;
  for (var i = 0; i < positions.length; i += 3) {
    positions[i] -= mx;
    positions[i + 1] -= my;
    positions[i + 2] -= mz;
  }
  var size = faces ? [maxX - minX, maxY - minY, maxZ - minZ] : [0, 0, 0];
  return { positions: positions, normals: normals, size: size };
}
