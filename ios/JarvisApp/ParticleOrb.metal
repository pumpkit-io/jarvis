// Shaders for PointOrbView: a sphere of points with a faint grid on the front face and a bright rim
// where the surface turns edge-on. Additive blending piles the rim points into a glow, so no depth buffer.

#include <metal_stdlib>
using namespace metal;

// Must match `OrbUniforms` in PointOrbView.swift field for field.
struct Uniforms {
    float time;        // rotation, twinkle and wisp drift
    float phase;       // surface noise time
    float level;
    float ripple;
    float flow;
    float looseShare;
    float looseDrift;
    float radiusPx;
    float viewW;
    float viewH;
    float dotPx;
};

// Must match `OrbPoint` in PointOrbView.swift.
struct OrbPoint {
    float4 position;   // unit sphere, w unused
    float4 random;     // three per-point values in 0...1, w unused
};

struct VOut {
    float4 position [[position]];
    float  pointSize [[point_size]];
    float  bright;
};

// Layered sines, roughly -1...1.
static float orbNoise(float3 p, float t) {
    float a = sin(p.x + t)             * cos(p.y * 1.3 - t * 0.7);
    float b = sin(p.y * 1.7 + t * 1.3) * cos(p.z * 1.1 + t * 0.5);
    float c = sin(p.z * 2.3 - t * 0.9) * cos(p.x * 1.9 + t * 1.1);
    return (a + b + c) / 3.0 * 1.6;
}

vertex VOut orbVertex(uint vid [[vertex_id]],
                      const device OrbPoint *points [[buffer(0)]],
                      constant Uniforms &u [[buffer(1)]]) {
    float3 p = points[vid].position.xyz;
    float h1 = points[vid].random.x, h2 = points[vid].random.y, h3 = points[vid].random.z;
    float t = u.phase;

    // Radial ripple: a broad swell plus finer bumps make the outline wavy.
    const float f1 = 3.0;
    float n1 = 0.6 * orbNoise(p * f1, t) + 0.8 * orbNoise(p * f1 * 2.6 + 3.3, t * 1.3);

    // Points slide along the surface: dark tears open where they pull apart.
    const float f2 = 5.0;
    float3 v = float3(orbNoise(p * f2 + 1.7, t * 0.8),
                      orbNoise(p * f2 + 5.3, t * 0.8),
                      orbNoise(p * f2 + 9.1, t * 0.8));
    float3 tangent = v - dot(v, p) * p;
    float3 q = p * (1.0 + u.ripple * n1) + tangent * u.flow;

    // Wisps: some points detach, drift outward and smear sideways.
    float edge  = 1.0 - u.looseShare;
    float loose = smoothstep(edge - 0.02, edge + 0.02, h1);
    // Drift rates step by 0.05 so time can wrap without a seam.
    float rate  = 0.5 + floor(h3 * 20.0) / 20.0;
    float drift = u.looseDrift * (0.3 + h2) * (0.6 + 0.4 * sin(u.time * rate + h1 * 20.0));
    q *= 1.0 + drift * loose;
    q += tangent * 0.06 * loose * (0.5 + u.level);

    // Slow rotation with a gentle tilt wobble.
    float ay = u.time * 0.15;
    float ax = 0.3 * sin(u.time * 0.2);
    float cy = cos(ay), sy = sin(ay), cx = cos(ax), sx = sin(ax);
    float x1 =  q.x * cy + q.z * sy;
    float z1 = -q.x * sy + q.z * cy;
    float y1 =  q.y * cx - z1 * sx;
    float z2 =  q.y * sx + z1 * cx;

    // Camera at z = 3; larger z2 is closer to the viewer.
    float s = 3.0 / (3.0 - z2);
    float2 off = float2(x1, y1) * u.radiusPx * s;

    float facing = z2 / max(length(q), 1e-4);
    float rim    = pow(1.0 - fabs(facing), 2.5);
    float front  = z2 > 0.0 ? 1.0 : 0.0;
    float b = (0.45 + 1.1 * rim) * (0.15 + 0.85 * front) * (0.75 + 0.5 * h2);
    b *= 1.0 + 0.3 * u.level;

    if (h3 > 0.996 && z2 > 0.0) {
        b = 2.2 * (0.6 + 0.4 * sin(u.time * 3.0 + h1 * 40.0));
    }

    VOut out;
    out.position  = float4(off.x / (u.viewW * 0.5), off.y / (u.viewH * 0.5), 0.5, 1.0);
    out.pointSize = max(2.0, u.dotPx * s);
    // Each point stays a crisp dot; brightness above full only comes from points overlapping.
    out.bright    = min(1.0, b * 0.9);
    return out;
}

fragment float4 orbFragment(VOut in [[stage_in]],
                            float2 pc [[point_coord]]) {
    float r = length(pc * 2.0 - 1.0);
    if (r > 1.0 || in.bright < 0.02) discard_fragment();
    float a = r < 0.75 ? 1.0 : 1.0 - (r - 0.75) / 0.25;
    return float4(float3(in.bright * a), 1.0);
}
