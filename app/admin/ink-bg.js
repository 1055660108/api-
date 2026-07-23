(function () {
  "use strict";

  const VERTEX_SHADER = `
    attribute vec2 aPosition;
    varying vec2 vUv;
    void main() {
      vUv = aPosition * 0.5 + 0.5;
      gl_Position = vec4(aPosition, 0.0, 1.0);
    }
  `;

  const FRAGMENT_SHADER = `
    precision highp float;
    varying vec2 vUv;
    uniform float uTime;
    uniform vec2 uResolution;
    uniform vec2 uCenter;
    uniform float uSize;
    uniform float uStrength;
    uniform float uSeed;
    uniform float uShatter;
    uniform float uNight;
    uniform float uVortex;

    float hash(vec2 p) {
      p += vec2(uSeed * 0.0137, uSeed * 0.0191);
      p = fract(p * vec2(123.34, 456.21));
      p += dot(p, p + 45.32);
      return fract(p.x * p.y);
    }

    float noise(vec2 p) {
      vec2 i = floor(p);
      vec2 f = fract(p);
      f = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);
      return mix(
        mix(hash(i), hash(i + vec2(1.0, 0.0)), f.x),
        mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), f.x),
        f.y
      );
    }

    float fbm(vec2 p) {
      float value = 0.0;
      float amplitude = 0.52;
      mat2 rotation = mat2(0.82, -0.57, 0.57, 0.82);
      for (int i = 0; i < 4; i++) {
        value += amplitude * noise(p);
        p = rotation * p * 2.03 + 9.17;
        amplitude *= 0.5;
      }
      return value;
    }

    void main() {
      vec2 point = vUv - uCenter;
      point.x *= uResolution.x / max(uResolution.y, 1.0);
      vec2 screenPoint = point;
      float screenRadius = length(screenPoint);
      float screenAngle = atan(screenPoint.y, screenPoint.x);
      float sector = floor((screenAngle + 3.14159265) / 6.2831853 * 15.0);
      float piece = hash(vec2(sector, floor(screenRadius / max(uSize * 0.17, 0.012))));
      float fracture = smoothstep(0.015, 0.62, uShatter);
      vec2 radialDirection = normalize(screenPoint + vec2(0.0001));
      vec2 tangentDirection = vec2(-radialDirection.y, radialDirection.x);
      vec2 fractureOffset = radialDirection * (0.018 + piece * 0.115) + tangentDirection * (piece - 0.5) * 0.065;
      point -= fractureOffset * fracture;
      float time = uTime * 0.105;
      float radius = length(point);
      float vortexRotation = uVortex * (
        time * 9.2 +
        (1.0 - smoothstep(0.0, max(uSize, 0.04), radius)) * 2.4 +
        (noise(point * 18.0 + time * 3.0) - 0.5) * 0.78
      );
      float vortexCosine = cos(vortexRotation);
      float vortexSine = sin(vortexRotation);
      point = mat2(vortexCosine, -vortexSine, vortexSine, vortexCosine) * point;
      float angle = atan(point.y, point.x);
      radius = length(point);

      float bend = sin(angle * 3.0 + time * 1.55) * 0.11 + noise(point * 2.0 + time) * 0.2;
      float cosine = cos(bend);
      float sine = sin(bend);
      vec2 swirl = mat2(cosine, -sine, sine, cosine) * point;
      float contourScale = mix(0.14, 1.0, smoothstep(0.045, 0.22, uSize));
      float contour = (
        noise(vec2(angle * 1.8, time * 0.95)) * 0.032 +
        sin(angle * 7.0 - time) * 0.007 +
        (noise(vec2(angle * 4.6 - time * 5.0, radius * 42.0 + time * 2.2)) - 0.5) * 0.022 * uVortex
      ) * contourScale;
      float sphere = smoothstep(uSize + contour + 0.014, uSize + contour - 0.016, radius);
      float sectorPosition = fract((screenAngle + 3.14159265) / 6.2831853 * 15.0);
      float angularCrack = 1.0 - smoothstep(0.0, 0.052, min(sectorPosition, 1.0 - sectorPosition));
      float ringPosition = fract(screenRadius / max(uSize, 0.04) * 3.35 + piece * 0.28);
      float ringCrack = 1.0 - smoothstep(0.0, 0.045, min(ringPosition, 1.0 - ringPosition));
      float crackMask = clamp(angularCrack + ringCrack * 0.42, 0.0, 1.0) * smoothstep(0.08, 0.58, uShatter);
      sphere *= 1.0 - crackMask * 0.96;

      float detailScale = mix(21.0, 5.8, smoothstep(0.05, 0.24, uSize));
      vec2 warp = vec2(
        noise(swirl * 3.4 + vec2(time * 0.38, -time * 0.22)),
        noise(swirl * 3.4 + vec2(7.1 - time * 0.27, 3.7 + time * 0.31))
      ) - 0.5;
      vec2 inkPoint = swirl + warp * mix(0.014, 0.072, smoothstep(0.05, 0.24, uSize));
      float flowTime = time * (1.0 + uVortex * 2.8);
      float cloud = fbm(inkPoint * detailScale + vec2(flowTime, -flowTime * 0.66));
      float folds = noise(inkPoint * detailScale * 1.88 - vec2(time * 1.06, time * 0.4));
      float vein = abs(noise(inkPoint * detailScale * 2.64 + vec2(-time * 0.42, time * 0.28)) - 0.5) * 2.0;
      float ring = sin(radius / max(uSize, 0.04) * 17.0 - cloud * 6.5 + time * 1.35) * 0.5 + 0.5;
      float pigment = smoothstep(0.2, 0.78, cloud * 0.68 + folds * 0.24 + ring * 0.08);
      pigment = clamp(pigment + smoothstep(0.58, 0.9, vein) * 0.16, 0.0, 1.0);

      vec2 spherePoint = point / max(uSize, 0.04);
      float depth = sqrt(max(0.0, 1.0 - dot(spherePoint, spherePoint)));
      vec3 normal = normalize(vec3(spherePoint.x, -spherePoint.y, depth));
      float light = dot(normal, normalize(vec3(-0.46, -0.52, 0.72))) * 0.5 + 0.5;
      float highlight = smoothstep(0.58, 0.94, light) * depth;
      float movingHighlight = pow(max(0.0, dot(normal, normalize(vec3(sin(time * 5.4) * 0.5 - 0.25, -0.58, 0.78)))), 12.0) * depth * uVortex;
      float shade = 1.0 - smoothstep(0.18, 0.72, light);
      float rim = pow(1.0 - depth, 1.65);

      vec3 wash = vec3(0.86, 0.87, 0.865);
      vec3 ink = vec3(0.008, 0.011, 0.012);
      vec3 color = mix(wash, ink, pigment);
      color = mix(color, vec3(0.965), highlight * (0.1 + (1.0 - pigment) * 0.1));
      color = mix(color, ink, shade * 0.16 + rim * 0.1);
      float caustic = pow(max(0.0, sin(angle * 5.0 - flowTime * 5.2) * 0.5 + 0.5), 5.0) * smoothstep(0.08, 0.88, depth);
      vec3 rainGlass = mix(vec3(0.22, 0.31, 0.33), vec3(0.008, 0.016, 0.019), pigment * 0.76 + shade * 0.13);
      rainGlass = mix(rainGlass, vec3(0.82, 0.91, 0.9), highlight * (0.28 + (1.0 - pigment) * 0.26));
      rainGlass = mix(rainGlass, vec3(0.99, 1.0, 1.0), movingHighlight * 0.62);
      rainGlass = mix(rainGlass, vec3(0.58, 0.72, 0.72), caustic * (0.13 + uVortex * 0.18));
      rainGlass = mix(rainGlass, vec3(0.08, 0.14, 0.15), rim * 0.22);
      color = mix(color, rainGlass, uNight);
      float grain = hash(gl_FragCoord.xy);
      float inkAlpha = 0.72 + pigment * 0.24 + depth * 0.02 + grain * 0.008;
      float waterAlpha = 0.5 + pigment * 0.3 + depth * 0.04 + rim * 0.08 + uVortex * 0.13;
      float alpha = sphere * mix(inkAlpha, waterAlpha, uNight) * uStrength;
      alpha *= 1.0 - smoothstep(0.56, 1.0, uShatter) * 0.92;

      vec2 groundPoint = vec2(
        point.x / max(uSize * 1.04, 0.01),
        (point.y + uSize * 0.98) / max(uSize * 0.22, 0.01)
      );
      float groundShadow = smoothstep(1.0, 0.0, length(groundPoint)) * (1.0 - sphere) * uStrength * 0.12;
      float halo = smoothstep(uSize + 0.095, uSize + 0.012, screenRadius) * (1.0 - sphere) * (1.0 - uShatter) * uNight * 0.09;
      vec3 groundColor = mix(vec3(0.05), vec3(0.36, 0.43, 0.43), uNight);
      vec3 finalColor = mix(groundColor, color, sphere);
      finalColor = mix(finalColor, vec3(0.72, 0.79, 0.79), halo * 0.72);
      gl_FragColor = vec4(finalColor, max(max(alpha, groundShadow), halo));
    }
  `;

  function compileShader(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const message = gl.getShaderInfoLog(shader) || "unknown shader error";
      gl.deleteShader(shader);
      throw new Error(message);
    }
    return shader;
  }

  function createProgram(gl) {
    const program = gl.createProgram();
    const vertex = compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER);
    const fragment = compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(program) || "unknown program error";
      gl.deleteProgram(program);
      throw new Error(message);
    }
    return program;
  }

  class HSInkBackground {
    constructor(canvas, options = {}) {
      this.canvas = canvas;
      this.kind = options.kind === "workspace" ? "workspace" : "entry";
      this.mode = this.kind === "workspace" ? "workspace" : "landing";
      this.active = true;
      this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.center = [0.5, 0.52];
      this.targetCenter = [0.5, 0.52];
      this.size = 0.3;
      this.targetSize = 0.3;
      this.strength = this.kind === "workspace" ? 0.34 : 1;
      this.targetStrength = this.strength;
      this.seed = Math.random() * 1000;
      this.startedAt = performance.now();
      this.growth = this.kind === "entry" ? 0.42 : 1;
      this.shatter = 0;
      this.burstStartedAt = 0;
      this.burstDuration = 2360;
      this.lastWidth = 0;
      this.lastHeight = 0;
      this.needsResize = true;
      this.frame = 0;

      try {
        this.gl = canvas.getContext("webgl", {
          alpha: true,
          antialias: false,
          depth: false,
          stencil: false,
          powerPreference: "high-performance",
          preserveDrawingBuffer: false,
        });
        if (!this.gl) throw new Error("WebGL unavailable");
        this.program = createProgram(this.gl);
        this.prepareGeometry();
        this.cacheLocations();
        this.gl.enable(this.gl.BLEND);
        this.gl.blendFunc(this.gl.SRC_ALPHA, this.gl.ONE_MINUS_SRC_ALPHA);
        this.resizeObserver = new ResizeObserver(() => { this.needsResize = true; });
        this.resizeObserver.observe(canvas.parentElement || canvas);
        this.setMode(this.mode, true);
        this.render = this.render.bind(this);
        this.frame = requestAnimationFrame(this.render);
      } catch (error) {
        canvas.classList.add("ink-canvas-fallback");
        console.warn("ink background disabled", error);
      }
    }

    prepareGeometry() {
      const gl = this.gl;
      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
      this.buffer = buffer;
    }

    cacheLocations() {
      const gl = this.gl;
      this.locations = {
        position: gl.getAttribLocation(this.program, "aPosition"),
        time: gl.getUniformLocation(this.program, "uTime"),
        resolution: gl.getUniformLocation(this.program, "uResolution"),
        center: gl.getUniformLocation(this.program, "uCenter"),
        size: gl.getUniformLocation(this.program, "uSize"),
        strength: gl.getUniformLocation(this.program, "uStrength"),
        seed: gl.getUniformLocation(this.program, "uSeed"),
        shatter: gl.getUniformLocation(this.program, "uShatter"),
        night: gl.getUniformLocation(this.program, "uNight"),
        vortex: gl.getUniformLocation(this.program, "uVortex"),
      };
    }

    dimensions() {
      const rect = this.canvas.getBoundingClientRect();
      return { width: Math.max(0, rect.width), height: Math.max(0, rect.height) };
    }

    resize() {
      if (!this.gl) return false;
      const { width, height } = this.dimensions();
      if (width < 2 || height < 2) return false;
      const compact = width < 720;
      const pixelRatio = Math.min(window.devicePixelRatio || 1, compact ? 1.25 : 1.5);
      const renderWidth = Math.max(1, Math.round(width * pixelRatio));
      const renderHeight = Math.max(1, Math.round(height * pixelRatio));
      if (this.canvas.width !== renderWidth || this.canvas.height !== renderHeight) {
        this.canvas.width = renderWidth;
        this.canvas.height = renderHeight;
        this.gl.viewport(0, 0, renderWidth, renderHeight);
      }
      this.lastWidth = width;
      this.lastHeight = height;
      this.needsResize = false;
      this.updateTargets(this.mode, false);
      return true;
    }

    updateTargets(mode, immediate) {
      const width = this.lastWidth || this.canvas.clientWidth || window.innerWidth;
      const height = this.lastHeight || this.canvas.clientHeight || window.innerHeight;
      const compact = width < 720;
      const aspect = width / Math.max(height, 1);
      if (mode === "landing") {
        this.targetCenter = [0.5, compact ? 0.55 : 0.52];
        this.targetSize = Math.min(compact ? 0.27 : 0.268, aspect * 0.43);
        this.targetStrength = 1;
      } else if (mode === "login" || mode === "converging") {
        this.targetCenter = [0.5, compact ? 0.78 : 0.8];
        this.targetSize = Math.min(0.047, aspect * 0.1);
        this.targetStrength = 0.94;
      } else {
        this.targetCenter = [compact ? 0.76 : 0.84, compact ? 0.78 : 0.72];
        this.targetSize = Math.min(compact ? 0.22 : 0.42, aspect * 0.48);
        this.targetStrength = 0.32;
      }
      if (immediate) {
        this.center = [...this.targetCenter];
        this.size = this.targetSize;
        this.strength = this.targetStrength;
      }
    }

    setMode(mode, immediate = false) {
      const previousMode = this.mode;
      this.mode = ["landing", "converging", "login", "workspace"].includes(mode) ? mode : "landing";
      this.updateTargets(this.mode, immediate);
      if (this.kind === "entry" && this.mode === "landing" && previousMode !== "landing") {
        this.growth = this.reducedMotion ? 1 : 0.42;
      }
      if (this.mode !== "landing") {
        this.burstStartedAt = 0;
        this.shatter = 0;
      }
    }

    setActive(active) {
      this.active = Boolean(active);
      if (this.active) this.needsResize = true;
    }

    randomize() {
      this.seed = Math.random() * 1000;
      this.startedAt = performance.now() - Math.random() * 8000;
      if (this.kind === "entry" && this.mode === "landing") this.growth = this.reducedMotion ? 1 : 0.42;
    }

    containsPoint(clientX, clientY) {
      if (!this.active || this.mode !== "landing" || !this.lastWidth || !this.lastHeight) return false;
      const centerX = this.center[0] * this.lastWidth;
      const centerY = (1 - this.center[1]) * this.lastHeight;
      const radius = this.size * this.lastHeight * Math.max(0.72, this.growth);
      return Math.hypot(clientX - centerX, clientY - centerY) <= radius * 1.06;
    }

    burstAt(clientX, clientY) {
      if (this.reducedMotion || this.burstStartedAt || !this.containsPoint(clientX, clientY)) return null;
      const centerX = this.center[0] * this.lastWidth;
      const centerY = (1 - this.center[1]) * this.lastHeight;
      const radius = this.size * this.lastHeight * Math.max(0.72, this.growth);
      this.burstStartedAt = performance.now();
      this.seed = Math.random() * 1000;
      return { x: centerX, y: centerY, radius };
    }

    render(now) {
      this.frame = requestAnimationFrame(this.render);
      if (!this.active || document.hidden || !this.gl || !this.canvas.isConnected) return;
      if (this.needsResize && !this.resize()) return;
      const ease = this.reducedMotion ? 1 : 0.075;
      this.center[0] += (this.targetCenter[0] - this.center[0]) * ease;
      this.center[1] += (this.targetCenter[1] - this.center[1]) * ease;
      this.size += (this.targetSize - this.size) * ease;
      this.strength += (this.targetStrength - this.strength) * ease;
      if (this.mode === "landing") this.growth += (1 - this.growth) * (this.reducedMotion ? 1 : 0.0085);
      else this.growth += (1 - this.growth) * 0.08;

      if (this.burstStartedAt) {
        const progress = Math.min(1, (now - this.burstStartedAt) / this.burstDuration);
        if (progress < 0.24) {
          const rise = progress / 0.24;
          this.shatter = 1 - Math.pow(1 - rise, 3);
        } else if (progress < 0.38) {
          this.shatter = 1;
        } else {
          const reform = (progress - 0.38) / 0.62;
          this.shatter = Math.pow(1 - reform, 2);
        }
        if (progress >= 1) {
          this.burstStartedAt = 0;
          this.shatter = 0;
        }
      }

      const timeSeconds = this.reducedMotion ? 0 : (now - this.startedAt) / 1000;
      const breathing = this.mode === "landing" && !this.reducedMotion ? 1 + Math.sin(timeSeconds * 0.42) * 0.018 : 1;
      const displaySize = this.size * this.growth * breathing;

      const gl = this.gl;
      gl.disable(gl.SCISSOR_TEST);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      const radiusPixels = Math.ceil((displaySize + 0.075 + this.shatter * 0.16) * this.canvas.height);
      const centerX = Math.round(this.center[0] * this.canvas.width);
      const centerY = Math.round(this.center[1] * this.canvas.height);
      const left = Math.max(0, centerX - radiusPixels);
      const bottom = Math.max(0, centerY - radiusPixels);
      const right = Math.min(this.canvas.width, centerX + radiusPixels);
      const top = Math.min(this.canvas.height, centerY + radiusPixels);
      gl.enable(gl.SCISSOR_TEST);
      gl.scissor(left, bottom, Math.max(1, right - left), Math.max(1, top - bottom));
      gl.useProgram(this.program);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      gl.enableVertexAttribArray(this.locations.position);
      gl.vertexAttribPointer(this.locations.position, 2, gl.FLOAT, false, 0, 0);
      gl.uniform1f(this.locations.time, timeSeconds);
      gl.uniform2f(this.locations.resolution, this.canvas.width, this.canvas.height);
      gl.uniform2f(this.locations.center, this.center[0], this.center[1]);
      gl.uniform1f(this.locations.size, displaySize);
      gl.uniform1f(this.locations.strength, this.strength);
      gl.uniform1f(this.locations.seed, this.seed);
      gl.uniform1f(this.locations.shatter, this.shatter);
      gl.uniform1f(this.locations.night, this.kind === "entry" ? 1 : 0);
      gl.uniform1f(this.locations.vortex, this.kind === "entry" && this.mode !== "landing" ? 1 : 0);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
      gl.disable(gl.SCISSOR_TEST);
    }
  }

  class HSRainScene {
    constructor(canvas) {
      this.canvas = canvas;
      this.context = canvas.getContext("2d", { alpha: true });
      this.active = true;
      this.mode = "landing";
      this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.pixelRatio = 1;
      this.width = 0;
      this.height = 0;
      this.wind = -24;
      this.drops = [];
      this.ripples = [];
      this.orbitDrops = [];
      this.burst = null;
      this.transitionStartedAt = 0;
      this.transitionDuration = 1250;
      this.lastFrameAt = performance.now();
      this.lastRenderAt = 0;
      this.frameInterval = 1000 / 60;
      this.needsResize = true;
      this.staticRendered = false;
      this.atmosphereCanvas = document.createElement("canvas");
      if (!this.context) return;
      this.resizeObserver = new ResizeObserver(() => { this.needsResize = true; });
      this.resizeObserver.observe(canvas.parentElement || canvas);
      this.render = this.render.bind(this);
      this.frame = requestAnimationFrame(this.render);
    }

    dimensions() {
      const rect = this.canvas.getBoundingClientRect();
      return { width: Math.max(1, rect.width || window.innerWidth), height: Math.max(1, rect.height || window.innerHeight) };
    }

    resize() {
      const { width, height } = this.dimensions();
      const compact = width < 720;
      this.pixelRatio = Math.min(window.devicePixelRatio || 1, compact ? 1 : 1.15);
      this.canvas.width = Math.max(1, Math.round(width * this.pixelRatio));
      this.canvas.height = Math.max(1, Math.round(height * this.pixelRatio));
      this.canvas.style.width = `${width}px`;
      this.canvas.style.height = `${height}px`;
      this.context.setTransform(this.pixelRatio, 0, 0, this.pixelRatio, 0, 0);
      this.width = width;
      this.height = height;
      this.needsResize = false;
      this.buildDrops();
      this.buildAtmosphere();
    }

    buildDrops() {
      const compact = this.width < 720;
      const count = compact ? 88 : Math.min(168, Math.max(128, Math.round(this.width * this.height / 9500)));
      this.drops = Array.from({ length: count }, () => this.createDrop(true));
      const orbitCount = compact ? 20 : 30;
      this.orbitDrops = Array.from({ length: orbitCount }, (_, index) => ({
        angle: index / orbitCount * Math.PI * 2 + Math.random() * 0.3,
        radius: 0.66 + Math.random() * 0.8,
        speed: (0.48 + Math.random() * 0.92) * (index % 2 ? 1 : -1),
        size: 1.2 + Math.random() * 4.2,
        lift: 0.28 + Math.random() * 0.62,
        phase: Math.random() * Math.PI * 2,
      }));
      this.ripples = [];
      this.staticRendered = false;
    }

    buildAtmosphere() {
      const canvas = this.atmosphereCanvas;
      canvas.width = Math.max(1, Math.round(this.width * this.pixelRatio));
      canvas.height = Math.max(1, Math.round(this.height * this.pixelRatio));
      const context = canvas.getContext("2d", { alpha: true });
      if (!context) return;
      context.setTransform(this.pixelRatio, 0, 0, this.pixelRatio, 0, 0);
      context.clearRect(0, 0, this.width, this.height);
      const horizon = context.createLinearGradient(0, 0, 0, this.height);
      horizon.addColorStop(0, "rgba(32, 43, 45, 0.08)");
      horizon.addColorStop(0.52, "rgba(108, 129, 128, 0.025)");
      horizon.addColorStop(0.82, "rgba(1, 5, 6, 0.02)");
      horizon.addColorStop(1, "rgba(178, 202, 199, 0.035)");
      context.fillStyle = horizon;
      context.fillRect(0, 0, this.width, this.height);
      const glow = context.createRadialGradient(this.width * 0.5, this.height * 0.5, 0, this.width * 0.5, this.height * 0.5, Math.max(this.width, this.height) * 0.56);
      glow.addColorStop(0, "rgba(154, 177, 176, 0.035)");
      glow.addColorStop(0.42, "rgba(83, 102, 103, 0.018)");
      glow.addColorStop(1, "rgba(0, 0, 0, 0)");
      context.fillStyle = glow;
      context.fillRect(0, 0, this.width, this.height);
    }

    createDrop(initial = false) {
      const depth = Math.random();
      const speed = 420 + depth * 940;
      return {
        x: Math.random() * (this.width + 180) - 90,
        y: initial ? Math.random() * this.height : -40 - Math.random() * this.height * 0.28,
        depth,
        speed,
        length: 12 + depth * 34 + Math.random() * 12,
        width: 0.5 + depth * 1.28,
        alpha: 0.08 + depth * 0.38,
        layer: Math.min(2, Math.floor(depth * 3)),
        phase: Math.random() * Math.PI * 2,
      };
    }

    resetDrop(drop) {
      Object.assign(drop, this.createDrop(false));
    }

    setMode(mode) {
      const nextMode = ["landing", "converging", "login"].includes(mode) ? mode : "login";
      if (nextMode === "converging" && this.mode !== "converging") {
        this.transitionStartedAt = performance.now();
        for (const drop of this.drops) {
          drop.gatherX = drop.x;
          drop.gatherY = drop.y;
          drop.gatherAngle = Math.atan2(drop.y - this.height * 0.2, drop.x - this.width * 0.5);
          drop.gatherTurns = 1.4 + Math.random() * 1.8;
        }
      }
      if (nextMode === "landing" && this.mode !== "landing") {
        this.transitionStartedAt = 0;
        this.buildDrops();
      }
      this.mode = nextMode;
      if (this.mode !== "landing") this.burst = null;
      this.staticRendered = false;
    }

    setActive(active) {
      this.active = Boolean(active);
      if (this.active) {
        this.needsResize = true;
        this.lastFrameAt = performance.now();
        this.lastRenderAt = 0;
      }
    }

    randomize() {
      this.wind = -38 + Math.random() * 58;
      if (this.width) this.buildDrops();
    }

    burstAt(x, y, radius) {
      if (this.reducedMotion || this.mode !== "landing") return;
      const particleCount = this.width < 720 ? 54 : 82;
      const particles = Array.from({ length: particleCount }, (_, index) => {
        const angle = (index / particleCount) * Math.PI * 2 + (Math.random() - 0.5) * 0.24;
        return {
          angle,
          distance: radius * (0.72 + Math.random() * 1.72),
          size: 1.5 + Math.random() * 5.8,
          stretch: 1.1 + Math.random() * 2.6,
          delay: Math.random() * 0.075,
          drift: (Math.random() - 0.5) * radius * 0.22,
        };
      });
      this.burst = { x, y, radius, startedAt: performance.now(), duration: 2360, particles };
    }

    drawMist(context, now) {
      context.save();
      context.globalAlpha = 0.82 + Math.sin(now * 0.00022) * 0.08;
      context.drawImage(this.atmosphereCanvas, 0, 0, this.width, this.height);
      context.restore();
    }

    drawRain(context, deltaSeconds) {
      context.save();
      context.lineCap = "round";
      const layerColors = ["rgba(138, 159, 160, .13)", "rgba(174, 197, 198, .24)", "rgba(218, 234, 233, .42)"];
      const layerWidths = [0.55, 0.95, 1.45];
      for (let layer = 0; layer < 3; layer += 1) {
        context.beginPath();
        for (const drop of this.drops) {
          if (drop.layer !== layer) continue;
          if (!this.reducedMotion) {
            drop.y += drop.speed * deltaSeconds;
            drop.x += this.wind * deltaSeconds * (0.32 + drop.depth * 0.68);
          }
          const slant = this.wind * 0.018 * drop.length;
          context.moveTo(drop.x, drop.y);
          context.lineTo(drop.x + slant, drop.y + drop.length);
          if (drop.y > this.height + drop.length || drop.x < -140 || drop.x > this.width + 140) {
            if (Math.random() < 0.3 && this.ripples.length < 32) {
              this.ripples.push({
                x: Math.max(0, Math.min(this.width, drop.x)),
                y: this.height * (0.91 + Math.random() * 0.075),
                age: 0,
                duration: 0.56 + Math.random() * 0.5,
                size: 8 + drop.depth * 20,
                spark: 2 + Math.floor(drop.depth * 4),
                phase: Math.random() * Math.PI,
              });
            }
            this.resetDrop(drop);
          }
        }
        context.strokeStyle = layerColors[layer];
        context.lineWidth = layerWidths[layer];
        context.shadowColor = layer === 2 ? "rgba(219, 240, 239, .22)" : "transparent";
        context.shadowBlur = layer === 2 ? 2.5 : 0;
        context.stroke();
      }
      context.restore();
    }

    drawRipples(context, deltaSeconds) {
      context.save();
      this.ripples = this.ripples.filter((ripple) => {
        ripple.age += deltaSeconds;
        const progress = ripple.age / ripple.duration;
        if (progress >= 1) return false;
        context.beginPath();
        context.ellipse(ripple.x, ripple.y, ripple.size * (0.5 + progress * 1.8), ripple.size * (0.08 + progress * 0.22), 0, 0, Math.PI * 2);
        context.strokeStyle = `rgba(191, 207, 206, ${(1 - progress) * 0.24})`;
        context.lineWidth = 0.7;
        context.stroke();
        context.beginPath();
        for (let index = 0; index < ripple.spark; index += 1) {
          const offset = (index - (ripple.spark - 1) / 2) * 2.8;
          const lift = Math.sin(Math.PI * progress) * (5 + index * 1.8);
          context.moveTo(ripple.x + offset, ripple.y - lift * 0.35);
          context.lineTo(ripple.x + offset * 1.25, ripple.y - lift);
        }
        context.strokeStyle = `rgba(213, 231, 229, ${(1 - progress) * 0.18})`;
        context.lineWidth = 0.55;
        context.stroke();
        return true;
      });
      context.restore();
    }

    loginTarget() {
      const compact = this.width < 720;
      return {
        x: this.width * 0.5,
        y: this.height * (compact ? 0.22 : 0.2),
        radius: Math.max(compact ? 26 : 32, this.height * 0.047),
      };
    }

    drawConvergence(context, now) {
      const target = this.loginTarget();
      const rawProgress = Math.min(1, Math.max(0, (now - this.transitionStartedAt) / this.transitionDuration));
      const progress = rawProgress * rawProgress * (3 - 2 * rawProgress);
      const fade = Math.max(0.08, 1 - rawProgress * 0.82);
      const layerColors = ["rgba(142, 169, 170, .16)", "rgba(184, 209, 209, .29)", "rgba(226, 241, 240, .48)"];
      context.save();
      context.lineCap = "round";
      for (let layer = 0; layer < 3; layer += 1) {
        context.beginPath();
        for (const drop of this.drops) {
          if (drop.layer !== layer) continue;
          const originX = Number.isFinite(drop.gatherX) ? drop.gatherX : drop.x;
          const originY = Number.isFinite(drop.gatherY) ? drop.gatherY : drop.y;
          const startRadius = Math.hypot(originX - target.x, originY - target.y);
          const startAngle = Number.isFinite(drop.gatherAngle) ? drop.gatherAngle : Math.atan2(originY - target.y, originX - target.x);
          const angle = startAngle + progress * Math.PI * 2 * (drop.gatherTurns || 2);
          const radius = startRadius * Math.pow(1 - progress, 1.08) + target.radius * 0.58 * Math.sin(progress * Math.PI);
          const x = target.x + Math.cos(angle) * radius;
          const y = target.y + Math.sin(angle) * radius * (0.68 + drop.depth * 0.12);
          const tangentX = -Math.sin(angle) * drop.length * (0.18 + fade * 0.42);
          const tangentY = Math.cos(angle) * drop.length * (0.18 + fade * 0.42);
          context.moveTo(x - tangentX, y - tangentY);
          context.lineTo(x, y);
        }
        context.strokeStyle = layerColors[layer];
        context.globalAlpha = fade;
        context.lineWidth = 0.55 + layer * 0.42;
        context.stroke();
      }
      context.restore();
      this.drawWaterOrbit(context, now, Math.min(1, rawProgress * 1.7));
    }

    drawWaterOrbit(context, now, strength = 1) {
      if (strength <= 0) return;
      const target = this.loginTarget();
      const time = now / 1000;
      context.save();
      context.lineCap = "round";

      context.beginPath();
      for (let index = 0; index < 14; index += 1) {
        const phase = (time * (0.62 + index % 3 * 0.08) + index / 14) % 1;
        const x = target.x + Math.sin(index * 2.73 + time * 1.3) * target.radius * (0.62 + index % 4 * 0.34);
        const y = target.y - target.radius * 2.3 + phase * target.radius * 4.7;
        context.moveTo(x, y - 4 - index % 4);
        context.lineTo(x - 1.2, y + 7 + index % 5);
      }
      context.strokeStyle = `rgba(164, 199, 200, ${0.18 * strength})`;
      context.lineWidth = 0.75;
      context.stroke();

      for (let index = 0; index < 4; index += 1) {
        const rotation = time * (0.7 + index * 0.17) * (index % 2 ? -1 : 1) + index * 0.9;
        context.beginPath();
        context.ellipse(target.x, target.y, target.radius * (1.05 + index * 0.22), target.radius * (0.3 + index * 0.055), rotation, 0.22, Math.PI * 1.62);
        context.strokeStyle = `rgba(170, 211, 211, ${(0.12 - index * 0.015) * strength})`;
        context.lineWidth = 0.65 + (3 - index) * 0.16;
        context.stroke();
      }

      for (const drop of this.orbitDrops) {
        const angle = drop.angle + time * drop.speed + Math.sin(time * 0.9 + drop.phase) * 0.24;
        const distance = target.radius * drop.radius * (1 + Math.sin(time * 1.7 + drop.phase) * 0.08);
        const x = target.x + Math.cos(angle) * distance * 1.42;
        const y = target.y + Math.sin(angle) * distance * drop.lift;
        const depth = Math.sin(angle) * 0.5 + 0.5;
        const size = drop.size * (0.68 + depth * 0.62) * strength;
        context.beginPath();
        context.arc(x, y, Math.max(0.4, size), 0, Math.PI * 2);
        context.fillStyle = `rgba(129, 177, 179, ${(0.2 + depth * 0.22) * strength})`;
        context.fill();
        context.beginPath();
        context.arc(x - size * 0.28, y - size * 0.34, Math.max(0.3, size * 0.22), 0, Math.PI * 2);
        context.fillStyle = `rgba(244, 251, 250, ${(0.32 + depth * 0.36) * strength})`;
        context.fill();
      }
      context.restore();
    }

    drawBurst(context, now) {
      if (!this.burst) return;
      const rawProgress = (now - this.burst.startedAt) / this.burst.duration;
      if (rawProgress >= 1) {
        this.burst = null;
        return;
      }
      const progress = Math.max(0, rawProgress);
      const travel = Math.sin(Math.PI * progress);
      const visibility = Math.sin(Math.PI * Math.min(1, progress * 1.08));
      context.save();
      context.lineCap = "round";
      for (const particle of this.burst.particles) {
        const localProgress = Math.max(0, Math.min(1, (progress - particle.delay) / (1 - particle.delay)));
        if (!localProgress) continue;
        const localTravel = Math.sin(Math.PI * localProgress);
        const distance = particle.distance * localTravel;
        const curve = Math.sin(localProgress * Math.PI * 2) * particle.drift;
        const x = this.burst.x + Math.cos(particle.angle) * distance + Math.sin(particle.angle) * curve;
        const y = this.burst.y + Math.sin(particle.angle) * distance + Math.abs(Math.sin(Math.PI * localProgress)) * this.burst.radius * 0.18;
        const tail = particle.stretch * (0.8 + travel * 2.2);
        context.beginPath();
        context.moveTo(x - Math.cos(particle.angle) * tail, y - Math.sin(particle.angle) * tail);
        context.lineTo(x, y);
        context.strokeStyle = `rgba(188, 220, 219, ${visibility * 0.34})`;
        context.lineWidth = Math.max(0.7, particle.size * 0.34);
        context.stroke();
        context.beginPath();
        context.ellipse(x, y, particle.size, particle.size * (0.62 + localTravel * 0.5), particle.angle, 0, Math.PI * 2);
        context.fillStyle = `rgba(159, 194, 194, ${visibility * 0.44})`;
        context.fill();
        context.beginPath();
        context.arc(x - particle.size * 0.25, y - particle.size * 0.28, Math.max(0.45, particle.size * 0.18), 0, Math.PI * 2);
        context.fillStyle = `rgba(244, 250, 248, ${visibility * 0.65})`;
        context.fill();
      }
      context.beginPath();
      context.ellipse(this.burst.x, this.burst.y, this.burst.radius * (0.25 + travel * 1.22), this.burst.radius * (0.08 + travel * 0.29), 0, 0, Math.PI * 2);
      context.strokeStyle = `rgba(176, 205, 204, ${(1 - travel * 0.45) * visibility * 0.26})`;
      context.lineWidth = 1.1;
      context.stroke();
      context.restore();
    }

    render(now) {
      this.frame = requestAnimationFrame(this.render);
      if (!this.context || !this.active || document.hidden || !this.canvas.isConnected) return;
      if (this.needsResize) this.resize();
      if (this.reducedMotion && this.staticRendered) return;
      if (!this.reducedMotion && now - this.lastRenderAt < this.frameInterval) return;
      const deltaSeconds = Math.min(0.04, Math.max(0.001, (now - this.lastFrameAt) / 1000));
      this.lastFrameAt = now;
      this.lastRenderAt = now;
      const context = this.context;
      context.clearRect(0, 0, this.width, this.height);
      if (this.mode === "landing") {
        this.drawMist(context, now);
        this.drawRain(context, deltaSeconds);
        this.drawRipples(context, deltaSeconds);
        this.drawBurst(context, now);
      } else if (this.mode === "converging") {
        this.drawMist(context, now);
        this.drawConvergence(context, now);
      } else {
        this.drawWaterOrbit(context, now, 1);
      }
      this.staticRendered = this.reducedMotion;
    }
  }

  window.HSInkBackground = HSInkBackground;
  window.HSRainScene = HSRainScene;
})();
