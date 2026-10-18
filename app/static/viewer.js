// 3D preview for any element with data-stl. Orbit: drag; zoom: wheel.
import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;

for (const el of document.querySelectorAll('.viewer[data-stl]')) {
  const height = el.clientHeight || 360;
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(el.clientWidth, height);
  el.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(dark ? 0x1b1b1a : 0xf3f3f1);
  const camera = new THREE.PerspectiveCamera(35, el.clientWidth / height, 0.1, 1e6);
  camera.up.set(0, 0, 1);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x555555, 2.0));
  const sun = new THREE.DirectionalLight(0xffffff, 1.5);
  sun.position.set(1, -1.5, 2);
  scene.add(sun);
  const controls = new OrbitControls(camera, renderer.domElement);
  // Render on demand (load, orbit, resize) instead of a continuous loop.
  const draw = () => renderer.render(scene, camera);
  controls.addEventListener('change', draw);

  new STLLoader().load(el.dataset.stl, (geo) => {
    geo.computeVertexNormals();
    geo.computeBoundingBox();
    const box = geo.boundingBox;
    const centre = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3()).length() || 1;
    const colour = el.dataset.state === 'failed' ? 0xc0675c : 0x8fa3b8;
    scene.add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: colour, metalness: 0.15, roughness: 0.6 })));
    scene.add(new THREE.LineSegments(new THREE.EdgesGeometry(geo, 25), new THREE.LineBasicMaterial({ color: dark ? 0xdddddd : 0x222222 })));
    controls.target.copy(centre);
    camera.position.copy(centre.clone().add(new THREE.Vector3(1, -1.4, 0.9).normalize().multiplyScalar(size * 1.5)));
    camera.near = size / 200;
    camera.far = size * 50;
    camera.updateProjectionMatrix();
    controls.update();
    draw();
  }, undefined, () => { el.textContent = 'Could not load the 3D preview.'; });

  new ResizeObserver(() => {
    renderer.setSize(el.clientWidth, height);
    camera.aspect = el.clientWidth / height;
    camera.updateProjectionMatrix();
    draw();
  }).observe(el);
}
