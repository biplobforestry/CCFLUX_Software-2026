// Scale bar, north arrow and a latitude/longitude graticule for every exported
// map. A map figure that leaves the screen has to stand on its own: without a
// scale nobody can say how far apart two plumes were, without an orientation
// mark nobody can say which way the airship flew, and without coordinates the
// figure cannot be tied to anything else in the campaign.
//
// Shared by the FLIR thermal map, the OPC and Partector size maps and the MIRO
// Rack trace-gas map, which compose their exports the same way: a canvas, a
// projection from latitude and longitude into it, and the bounds it covers.
//
// Everything is laid out in points against the seven-inch figure width, so the
// furniture is the same physical size whatever resolution was asked for and
// nothing falls below nine point.
(function (global) {
  'use strict';

  const FIGURE_WIDTH_INCHES = 7;
  const MINIMUM_POINTS = 9;
  const INK = '#07182a';
  const PAPER = '#ffffff';
  // 1, 2 and 5 across the decades: the intervals a reader can divide by eye.
  const STEPS = [1, 2, 5];

  function niceBelow(value) {
    if (!(value > 0)) return 0;
    const decade = Math.pow(10, Math.floor(Math.log10(value)));
    let best = decade;
    STEPS.forEach(step => { if (step * decade <= value) best = step * decade; });
    return best;
  }

  // The largest 1/2/5 step that still puts at least `least` lines in the span.
  function graticuleStep(span, least) {
    if (!(span > 0)) return 0;
    let step = niceBelow(span / Math.max(1, least));
    // A span narrower than a thousandth of a degree is a hover, not a flight.
    return step < 1e-4 ? 1e-4 : step;
  }

  function decimalsFor(step) {
    if (step >= 1) return 0;
    return Math.min(4, Math.ceil(-Math.log10(step)));
  }

  function halo(context, text, x, y, weight) {
    context.lineJoin = 'round';
    context.miterLimit = 2;
    context.strokeStyle = PAPER;
    context.lineWidth = weight;
    context.strokeText(text, x, y);
    context.fillStyle = INK;
    context.fillText(text, x, y);
  }

  function drawGraticule(context, options, pt) {
    const {width, height, bounds, project} = options;
    const latStep = graticuleStep(Math.abs(bounds.north - bounds.south), 3);
    const lonStep = graticuleStep(Math.abs(bounds.east - bounds.west), 3);
    if (!latStep || !lonStep) return {latStep: 0, lonStep: 0};
    context.save();
    context.setLineDash([4 * pt, 4 * pt]);
    context.strokeStyle = 'rgba(7,24,42,.34)';
    context.lineWidth = 0.6 * pt;
    context.font = `${MINIMUM_POINTS * pt}px Arial`;

    const labels = [];
    for (
      let lat = Math.ceil(bounds.south / latStep) * latStep;
      lat <= bounds.north;
      lat += latStep
    ) {
      const at = project(lat, bounds.west);
      if (at.y < 0 || at.y > height) continue;
      context.beginPath();
      context.moveTo(0, at.y);
      context.lineTo(width, at.y);
      context.stroke();
      labels.push([
        `${Math.abs(lat).toFixed(decimalsFor(latStep))}°${lat < 0 ? 'S' : 'N'}`,
        6 * pt, at.y - 3 * pt
      ]);
    }
    for (
      let lon = Math.ceil(bounds.west / lonStep) * lonStep;
      lon <= bounds.east;
      lon += lonStep
    ) {
      const at = project(bounds.south, lon);
      if (at.x < 0 || at.x > width) continue;
      context.beginPath();
      context.moveTo(at.x, 0);
      context.lineTo(at.x, height);
      context.stroke();
      labels.push([
        `${Math.abs(lon).toFixed(decimalsFor(lonStep))}°${lon < 0 ? 'W' : 'E'}`,
        at.x + 3 * pt, height - 6 * pt
      ]);
    }
    // Labels after every line, so a line never prints over a label.
    context.setLineDash([]);
    labels.forEach(([text, x, y]) => halo(context, text, x, y, 2.6 * pt));
    context.restore();
    return {latStep, lonStep};
  }

  function drawNorthArrow(context, options, pt) {
    const x = 18 * pt;
    const y = 16 * pt;
    const stem = 26 * pt;
    const half = 6 * pt;
    context.save();
    context.beginPath();
    context.moveTo(x, y);
    context.lineTo(x + half, y + stem);
    context.lineTo(x, y + stem * 0.74);
    context.lineTo(x - half, y + stem);
    context.closePath();
    context.fillStyle = INK;
    context.strokeStyle = PAPER;
    context.lineWidth = 1.8 * pt;
    context.stroke();
    context.fill();
    context.font = `700 ${(MINIMUM_POINTS + 1) * pt}px Arial`;
    context.textAlign = 'center';
    halo(context, 'N', x, y + stem + 11 * pt, 2.6 * pt);
    context.restore();
  }

  function formatDistance(metres) {
    if (metres >= 1000) {
      const km = metres / 1000;
      return `${km >= 10 ? km.toFixed(0) : km.toFixed(1)} km`;
    }
    return `${metres.toFixed(0)} m`;
  }

  function drawScaleBar(context, options, pt) {
    const {width, height, metresPerPixel} = options;
    if (!(metresPerPixel > 0)) return null;
    // About a fifth of the figure, rounded down to a readable distance.
    const metres = niceBelow(width * 0.2 * metresPerPixel);
    if (!metres) return null;
    const length = metres / metresPerPixel;
    const bar = 5 * pt;
    const right = width - 18 * pt;
    const left = right - length;
    const y = height - 20 * pt;
    context.save();
    // Two alternating halves, the convention that lets a reader halve it.
    context.fillStyle = INK;
    context.fillRect(left, y, length / 2, bar);
    context.fillStyle = PAPER;
    context.fillRect(left + length / 2, y, length / 2, bar);
    context.strokeStyle = INK;
    context.lineWidth = 0.75 * pt;
    context.strokeRect(left, y, length, bar);
    context.font = `${MINIMUM_POINTS * pt}px Arial`;
    context.textAlign = 'center';
    halo(context, '0', left, y - 4 * pt, 2.6 * pt);
    halo(context, formatDistance(metres), right, y - 4 * pt, 2.6 * pt);
    context.restore();
    return metres;
  }

  // Metres per pixel for a Web Mercator tile pyramid at this latitude and zoom.
  // Mercator stretches with latitude, so the ground distance a pixel covers is
  // the equatorial figure narrowed by cos(latitude).
  function metresPerPixel(latitude, zoom) {
    return 156543.03392804097 * Math.cos(latitude * Math.PI / 180)
      / Math.pow(2, zoom);
  }

  /**
   * Draw the furniture every exported map carries.
   *
   * options.width/height  canvas drawing units (after any context.scale)
   * options.project       (lat, lon) -> {x, y} in those units
   * options.bounds        {north, south, east, west} the canvas covers
   * options.metresPerPixel  ground metres per drawing unit
   */
  function draw(context, options) {
    if (!context || !options || !options.width || !options.height) return null;
    const pt = options.width / (FIGURE_WIDTH_INCHES * 72);
    const previousAlign = context.textAlign;
    const previousBaseline = context.textBaseline;
    context.textBaseline = 'alphabetic';
    const graticule = options.bounds && options.project
      ? drawGraticule(context, options, pt)
      : {latStep: 0, lonStep: 0};
    context.textAlign = 'left';
    drawNorthArrow(context, options, pt);
    const scale = drawScaleBar(context, options, pt);
    context.textAlign = previousAlign;
    context.textBaseline = previousBaseline;
    return {...graticule, scaleMetres: scale, pointSize: pt};
  }

  /**
   * Only the coordinate grid. The MIRO Rack map composes its own scale bar and
   * north arrow around a header and footer this cannot see, and already labels
   * its corners; it needs the graticule and nothing else.
   */
  function graticule(context, options) {
    if (!context || !options || !options.width || !options.height) return null;
    if (!options.bounds || !options.project) return null;
    const pt = options.width / (FIGURE_WIDTH_INCHES * 72);
    const previousAlign = context.textAlign;
    const previousBaseline = context.textBaseline;
    context.textAlign = 'left';
    context.textBaseline = 'alphabetic';
    const result = drawGraticule(context, options, pt);
    context.textAlign = previousAlign;
    context.textBaseline = previousBaseline;
    return result;
  }

  global.CCFLUXMapFurniture = {
    draw, graticule, metresPerPixel, niceBelow, graticuleStep, decimalsFor,
    FIGURE_WIDTH_INCHES, MINIMUM_POINTS
  };
})(typeof window !== 'undefined' ? window : globalThis);
