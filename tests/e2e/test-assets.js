// Deterministic binary test assets for the E2E upload tests. Everything is
// generated in-memory (no files on disk), so `setInputFiles` can feed real
// byte streams for PNG/JPEG/PDF/DOCX/XLSX straight to the upload handler.

export const RED_PNG_B64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGM8ISfHgA0wYRUdtBIA0MoBFD5jqJkAAAAASUVORK5CYII=';

export const SAMPLE_JPG_B64 =
  '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDwKiiivzI/uM//2Q==';

export function pngFile() {
  return {
    name: 'red-dot.png',
    mimeType: 'image/png',
    buffer: Buffer.from(RED_PNG_B64, 'base64'),
  };
}

export function jpegFile() {
  return {
    name: 'sample.jpg',
    mimeType: 'image/jpeg',
    buffer: Buffer.from(SAMPLE_JPG_B64, 'base64'),
  };
}

export function codeFile() {
  return {
    name: 'sample.py',
    mimeType: 'text/x-python',
    buffer: Buffer.from(
      '# E2E sample code file\n' +
        'def greet(name):\n' +
        '    return f"Hello, {name}!"\n' +
        '\n' +
        'print(greet("World"))\n'
    ),
  };
}

// ---------- ZIP writer (stored entries, CRC32) ----------

let _crcTable = null;
function crc32(buf) {
  if (!_crcTable) {
    _crcTable = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? (0xedb88320 ^ (c >>> 1)) : c >>> 1;
      _crcTable[n] = c >>> 0;
    }
  }
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = _crcTable[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

class ByteWriter {
  constructor() {
    this.chunks = [];
    this.length = 0;
  }
  u8(v) {
    this.chunks.push(Uint8Array.of(v & 0xff));
    this.length += 1;
  }
  u16(v) {
    this.u8(v);
    this.u8(v >> 8);
  }
  u32(v) {
    this.u16(v);
    this.u16(v >>> 16);
  }
  raw(u8arr) {
    this.chunks.push(u8arr);
    this.length += u8arr.length;
  }
  text(s) {
    this.raw(new TextEncoder().encode(s));
  }
  bytes() {
    const out = new Uint8Array(this.length);
    let off = 0;
    for (const c of this.chunks) {
      out.set(c, off);
      off += c.length;
    }
    return out;
  }
}

function makeZip(entries) {
  const w = new ByteWriter();
  const localOffsets = [];
  for (const { name, data } of entries) {
    const nameBytes = new TextEncoder().encode(name);
    const crc = crc32(data);
    localOffsets.push(w.length);
    w.u32(0x04034b50); // local file header signature
    w.u16(20); // version needed
    w.u16(0); // flags
    w.u16(0); // method: stored
    w.u16(0x21); // mod time
    w.u16(0x5821); // mod date
    w.u32(crc);
    w.u32(data.length);
    w.u32(data.length);
    w.u16(nameBytes.length);
    w.u16(0); // extra len
    w.raw(nameBytes);
    w.raw(data);
  }
  const cdStart = w.length;
  for (let i = 0; i < entries.length; i++) {
    const { name, data } = entries[i];
    const nameBytes = new TextEncoder().encode(name);
    const crc = crc32(data);
    w.u32(0x02014b50); // central directory header signature
    w.u16(20); // version made by
    w.u16(20); // version needed
    w.u16(0);
    w.u16(0);
    w.u16(0x21);
    w.u16(0x5821);
    w.u32(crc);
    w.u32(data.length);
    w.u32(data.length);
    w.u16(nameBytes.length);
    w.u16(0); // extra
    w.u16(0); // comment
    w.u16(0); // disk
    w.u16(0); // internal attrs
    w.u32(0); // external attrs
    w.u32(localOffsets[i]);
    w.raw(nameBytes);
  }
  const cdSize = w.length - cdStart;
  w.u32(0x06054b50); // EOCD
  w.u16(0);
  w.u16(0);
  w.u16(entries.length);
  w.u16(entries.length);
  w.u32(cdSize);
  w.u32(cdStart);
  w.u16(0);
  return w.bytes();
}

export function docxFile() {
  const data = makeZip([
    {
      name: '[Content_Types].xml',
      data: new TextEncoder().encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
          '<Default Extension="xml" ContentType="application/xml"/>' +
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' +
          '</Types>'
      ),
    },
    {
      name: '_rels/.rels',
      data: new TextEncoder().encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
          '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>' +
          '</Relationships>'
      ),
    },
    {
      name: 'word/document.xml',
      data: new TextEncoder().encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
          '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' +
          '<w:body><w:p><w:r><w:t>E2E DOCX upload test</w:t></w:r></w:p></w:body>' +
          '</w:document>'
      ),
    },
  ]);
  return {
    name: 'notes.docx',
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    buffer: Buffer.from(data),
  };
}

export function xlsxFile() {
  const data = makeZip([
    {
      name: '[Content_Types].xml',
      data: new TextEncoder().encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
          '<Default Extension="xml" ContentType="application/xml"/>' +
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
          '</Types>'
      ),
    },
    {
      name: '_rels/.rels',
      data: new TextEncoder().encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
          '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
          '</Relationships>'
      ),
    },
    {
      name: 'xl/workbook.xml',
      data: new TextEncoder().encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
          '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>' +
          '</workbook>'
      ),
    },
    {
      name: 'xl/_rels/workbook.xml.rels',
      data: new TextEncoder().encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
          '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' +
          '</Relationships>'
      ),
    },
    {
      name: 'xl/worksheets/sheet1.xml',
      data: new TextEncoder().encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
          '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
          '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>E2E XLSX upload test</t></is></c></row></sheetData>' +
          '</worksheet>'
      ),
    },
  ]);
  return {
    name: 'data.xlsx',
    mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    buffer: Buffer.from(data),
  };
}

// ---------- PDF writer ----------

export function pdfFile({ embedImage = false } = {}) {
  const jpegBytes = Buffer.from(SAMPLE_JPG_B64, 'base64');

  // Objects 1..4 are pure text; track their byte offsets for the xref table.
  const textObjects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    embedImage
      ? '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>'
      : '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> /Contents 4 0 R >>',
    (() => {
      const content = embedImage ? 'q 100 0 0 100 50 50 cm /Im0 Do Q' : 'q Q';
      return `<< /Length ${content.length} >>\nstream\n${content}\nendstream`;
    })(),
  ];

  let header = '%PDF-1.4\n';
  const offsets = [];
  for (let i = 0; i < textObjects.length; i++) {
    offsets.push(header.length);
    header += `${i + 1} 0 obj\n${textObjects[i]}\nendobj\n`;
  }

  const obj5Header =
    `5 0 obj\n<< /Type /XObject /Subtype /Image /Width 8 /Height 8 ` +
    `/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${jpegBytes.length} >>\nstream\n`;
  const obj5Tail = '\nendstream\nendobj\n';
  offsets.push(header.length); // object 5 starts right after the text objects

  const xrefStart = header.length + obj5Header.length + jpegBytes.length + obj5Tail.length;

  let xref = `xref\n0 ${offsets.length + 1}\n`;
  xref += '0000000000 65535 f \n';
  for (const off of offsets) {
    xref += String(off).padStart(10, '0') + ' 00000 n \n';
  }
  const trailer = `trailer\n<< /Size ${offsets.length + 1} /Root 1 0 R >>\nstartxref\n${xrefStart}\n%%EOF\n`;

  const buf = Buffer.alloc(xrefStart + Buffer.byteLength(xref + trailer));
  buf.write(header + obj5Header, 0, 'utf8');
  jpegBytes.copy(buf, Buffer.byteLength(header + obj5Header));
  buf.write(obj5Tail, Buffer.byteLength(header + obj5Header) + jpegBytes.length, 'utf8');
  buf.write(xref + trailer, xrefStart, 'utf8');

  return {
    name: embedImage ? 'report-with-image.pdf' : 'report.pdf',
    mimeType: 'application/pdf',
    buffer: buf,
  };
}
