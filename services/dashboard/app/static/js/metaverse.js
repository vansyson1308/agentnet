import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

// --- Setup Scene ---
const container = document.getElementById('three-canvas');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0f172a); // dark blue-gray

// --- Camera ---
const camera = new THREE.PerspectiveCamera(60, container.clientWidth / container.clientHeight, 0.1, 1000);
camera.position.set(15, 10, 15);
camera.lookAt(0, 0, 0);

// --- Renderers ---
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(container.clientWidth, container.clientHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
container.appendChild(renderer.domElement);

const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(container.clientWidth, container.clientHeight);
labelRenderer.domElement.style.position = 'absolute';
labelRenderer.domElement.style.top = '0';
labelRenderer.domElement.style.pointerEvents = 'none'; // allow clicking through to canvas
container.appendChild(labelRenderer.domElement);

// --- Controls ---
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.05;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.5;
controls.target.set(0, 2, 0);

// --- Lights ---
const ambientLight = new THREE.AmbientLight(0x404060);
scene.add(ambientLight);

const dirLight = new THREE.DirectionalLight(0xffffff, 1.2);
dirLight.position.set(5, 10, 7);
dirLight.castShadow = true;
scene.add(dirLight);

const fillLight = new THREE.DirectionalLight(0x8888ff, 0.3);
fillLight.position.set(-5, 0, 5);
scene.add(fillLight);

// --- Ground ---
const groundGeometry = new THREE.PlaneGeometry(30, 30);
const groundMaterial = new THREE.MeshStandardMaterial({
    color: 0x1e293b,
    roughness: 0.8,
    metalness: 0.2,
    transparent: true,
    opacity: 0.6
});
const ground = new THREE.Mesh(groundGeometry, groundMaterial);
ground.rotation.x = -Math.PI / 2;
ground.position.y = 0;
ground.receiveShadow = true;
scene.add(ground);

// Grid helper
const gridHelper = new THREE.GridHelper(30, 20, 0x334155, 0x1e293b);
gridHelper.position.y = 0.01;
scene.add(gridHelper);

// --- Agent Avatars (cube + label) ---
const agentNames = [
    'TranslatorBot', 'DataMiner', 'WebScaper', 'SentimentAI',
    'ImageGen', 'CodeReview', 'PlannerAlpha', 'OracleV1'
];
const colors = [0x3b82f6, 0x10b981, 0xf59e0b, 0xef4444, 0x8b5cf6, 0x06b6d4, 0xd946ef, 0x14b8a6];
const agentMeshes = [];

// Random positions
const positions = [];
for (let i = 0; i < agentNames.length; i++) {
    let x, z;
    do {
        x = (Math.random() - 0.5) * 20;
        z = (Math.random() - 0.5) * 20;
    } while (Math.abs(x) < 2 && Math.abs(z) < 2); // keep away from center
    positions.push({ x, z });
    createAgent(x, z, agentNames[i], colors[i % colors.length]);
}

function createAgent(x, z, name, color) {
    // Main cube
    const geometry = new THREE.BoxGeometry(0.8, 0.8, 0.8);
    const material = new THREE.MeshStandardMaterial({
        color: color,
        emissive: color,
        emissiveIntensity: 0.1,
        roughness: 0.3,
        metalness: 0.7
    });
    const cube = new THREE.Mesh(geometry, material);
    cube.position.set(x, 0.4, z);
    cube.castShadow = true;
    cube.receiveShadow = true;
    scene.add(cube);
    agentMeshes.push(cube);

    // Floating particle ring (simple ring of small spheres)
    const ringGroup = new THREE.Group();
    const particleCount = 8;
    for (let i = 0; i < particleCount; i++) {
        const angle = (i / particleCount) * Math.PI * 2;
        const particle = new THREE.Mesh(
            new THREE.SphereGeometry(0.08, 6, 6),
            new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.6 })
        );
        particle.position.set(Math.cos(angle) * 1.2, 0, Math.sin(angle) * 1.2);
        ringGroup.add(particle);
    }
    ringGroup.position.set(x, 0.4, z);
    ringGroup.userData = { baseY: 0.4, time: Math.random() * 100 };
    scene.add(ringGroup);

    // 3D label (CSS2D)
    const labelDiv = document.createElement('div');
    labelDiv.className = 'agent-label';
    labelDiv.textContent = name;
    labelDiv.style.background = `rgba(${parseInt(color >> 16)}, ${parseInt((color >> 8) & 0xff)}, ${parseInt(color & 0xff)}, 0.7)`;
    labelDiv.style.borderColor = `#${color.toString(16).padStart(6, '0')}`;
    const label = new CSS2DObject(labelDiv);
    label.position.set(x, 1.5, z);
    scene.add(label);
}

// Update agent count display
document.getElementById('agent-count').textContent = agentNames.length;

// --- Animation Loop ---
function animate() {
    requestAnimationFrame(animate);

    // Rotate cubes
    const time = Date.now() * 0.001;
    agentMeshes.forEach((mesh, i) => {
        mesh.rotation.x += 0.01;
        mesh.rotation.y += 0.02;
        // Slight vertical bob for each agent
        mesh.position.y = 0.4 + Math.sin(time * 2 + i) * 0.1;
    });

    // Animate rings
    scene.children.forEach(child => {
        if (child.userData && child.userData.baseY !== undefined) {
            child.rotation.y += 0.01;
            child.position.y = child.userData.baseY + Math.sin(time * 2 + child.userData.time) * 0.15;
        }
    });

    controls.update();

    renderer.render(scene, camera);
    labelRenderer.render(scene, camera);
}

animate();

// --- Resize Handler ---
window.addEventListener('resize', () => {
    const width = container.clientWidth;
    const height = container.clientHeight;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
    labelRenderer.setSize(width, height);
});