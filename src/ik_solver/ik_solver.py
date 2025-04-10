import pybullet as p
import pybullet_data
import numpy as np



class DifferentialIKSolver:
    def __init__(self, robot_id, ee_link_index, damping=0.001, use_shadow_client=True):
        self.robot_id = robot_id
        self.ee_link_index = ee_link_index
        self.damping = damping
        self.use_shadow_client = use_shadow_client # whether to use shadow client for IK calculations
        
        self.joint_indices = []
        for i in range(p.getNumJoints(robot_id)):
            joint_info = p.getJointInfo(robot_id, i)
            if joint_info[2] == p.JOINT_REVOLUTE:
                self.joint_indices.append(i)
        self.num_joints = len(self.joint_indices)
        
        # Create shadow client for IK calculations if needed
        if self.use_shadow_client:
            self.shadow_client_id = p.connect(p.DIRECT)
            
            robot_pos, robot_orn = p.getBasePositionAndOrientation(self.robot_id)
            
            # Load the same robot in the shadow client with the same position and orientation
            p.setAdditionalSearchPath(pybullet_data.getDataPath(), physicsClientId=self.shadow_client_id)
            self.shadow_robot_id = p.loadURDF("franka_panda/panda.urdf", 
                                             basePosition=robot_pos,
                                             baseOrientation=robot_orn,
                                             useFixedBase=True, 
                                             physicsClientId=self.shadow_client_id)
            
            # Copy the current joint states from the real robot to the shadow robot
            for joint_idx in self.joint_indices:
                joint_state = p.getJointState(self.robot_id, joint_idx)
                joint_pos = joint_state[0]
                joint_vel = 0.0
                p.resetJointState(self.shadow_robot_id, joint_idx, 
                                 targetValue=joint_pos,
                                 targetVelocity=joint_vel,
                                 physicsClientId=self.shadow_client_id)
        else:
            self.shadow_client_id = None
            self.shadow_robot_id = None
        
        print(f"\nRobot Configuration:")
        print(f"Number of controlled joints: {self.num_joints}")
        print(f"Joint indices: {self.joint_indices}")
        
        if self.use_shadow_client:
            print(f"Shadow client initialized with ID: {self.shadow_client_id}")
            print(f"Shadow robot initialized with ID: {self.shadow_robot_id}")

    def get_current_ee_pose(self, use_shadow=False):
        """Get the current end effector pose"""
        if use_shadow and self.use_shadow_client:
            client_id = self.shadow_client_id
            robot_id = self.shadow_robot_id
        else:
            client_id = 0
            robot_id = self.robot_id
        
        p.setPhysicsEngineParameter(enableConeFriction=0, physicsClientId=client_id)
        
        ee_state = p.getLinkState(robot_id, self.ee_link_index, physicsClientId=client_id)
        return np.array(ee_state[0]), np.array(ee_state[1]) # pos and ori

    def get_jacobian(self, joint_positions, use_shadow=True):
        """Calculate the Jacobian matrix"""
        delta = 1e-3
        jac = np.zeros((6, len(self.joint_indices)))
        
        if use_shadow and self.use_shadow_client:
            client_id = self.shadow_client_id
            robot_id = self.shadow_robot_id
        else:
            client_id = 0
            robot_id = self.robot_id
        
        p.setPhysicsEngineParameter(enableConeFriction=0, physicsClientId=client_id)
        
        original_pos = joint_positions.copy()
        
        # Set initial joint positions
        for idx, pos in zip(self.joint_indices, original_pos):
            p.resetJointState(robot_id, idx, pos, physicsClientId=client_id)
            
        current_pos, current_orn = self.get_current_ee_pose(use_shadow=use_shadow)
        
        for i in range(len(self.joint_indices)):
            joint_positions = original_pos.copy()
            joint_positions[i] += delta
            
            # set joint state
            for idx, pos in zip(self.joint_indices, joint_positions):
                p.resetJointState(robot_id, idx, pos, physicsClientId=client_id)
            
            # new position and orientation
            new_pos, new_orn = self.get_current_ee_pose(use_shadow=use_shadow)
            
            # pos jacobian 
            jac[:3, i] = (new_pos - current_pos) / delta
            
            # ori jacobian 
            # quaternion difference as angular velocity
            orn_diff = p.getDifferenceQuaternion(current_orn.tolist(), new_orn.tolist(), physicsClientId=client_id)
            jac[3:, i] = np.array(orn_diff[:3]) / delta
        
        # reset joint state
        for idx, pos in zip(self.joint_indices, original_pos):
            p.resetJointState(robot_id, idx, pos, physicsClientId=client_id)
            
        return jac

    def solve(self, target_pos, target_orn, current_joint_positions, max_iters=50, tolerance=1e-3):
        """solve IK using shadow client if available"""
        current_joints = np.array(current_joint_positions)

        # define joint limits of Franka Panda robot
        joint_limits = [
            (-2.9671, 2.9671),  # Joint 1 (panda_joint1)
            (-1.8326, 1.8326),  # Joint 2 (panda_joint2)
            (-2.9671, 2.9671),  # Joint 3 (panda_joint3)
            (-3.1416, 0.0),     # Joint 4 (panda_joint4)
            (-2.9671, 2.9671),  # Joint 5 (panda_joint5)
            (-0.0873, 3.8223),  # Joint 6 (panda_joint6)
            (-2.9671, 2.9671),  # Joint 7 (panda_joint7)
        ]
        
        # If using shadow client, set initial joint positions in shadow robot
        if self.use_shadow_client:
            for i, joint_idx in enumerate(self.joint_indices):
                p.resetJointState(self.shadow_robot_id, joint_idx, current_joints[i], 
                                 physicsClientId=self.shadow_client_id)
        
        for iter in range(max_iters):
            # Use shadow client for calculations if available
            current_pos, current_orn = self.get_current_ee_pose(use_shadow=self.use_shadow_client)
            
            pos_error = target_pos - current_pos
            pos_error_norm = np.linalg.norm(pos_error)
            
            client_id = self.shadow_client_id if self.use_shadow_client else 0
            orn_error = np.array(p.getDifferenceQuaternion(current_orn.tolist(), target_orn, 
                                                          physicsClientId=client_id)[:3])
            orn_error_norm = np.linalg.norm(orn_error)
            
            # combine position and orientation error
            error = np.concatenate([pos_error, orn_error])
            print(f"Iteration {iter}, Position Error: {pos_error_norm:.6f}, Orientation Error: {orn_error_norm:.6f}")
            
            if pos_error_norm < tolerance and orn_error_norm < tolerance:
                print("IK solved successfully!")
                break
            
            J = self.get_jacobian(current_joints, use_shadow=self.use_shadow_client)
            
            # damped least squares
            delta_q = np.linalg.solve(
                J.T @ J + self.damping * np.eye(self.num_joints),
                J.T @ error
            )
            
            # update joint angles, and check joint limits
            new_joints = current_joints + delta_q
            
            # apply joint limits
            for i in range(min(len(new_joints), len(joint_limits))):
                lower_limit, upper_limit = joint_limits[i]
                if new_joints[i] < lower_limit:
                    new_joints[i] = lower_limit
                    print(f"Warning: Joint {i+1} exceeds lower limit, truncated to {lower_limit}")
                elif new_joints[i] > upper_limit:
                    new_joints[i] = upper_limit
                    print(f"Warning: Joint {i+1} exceeds upper limit, truncated to {upper_limit}")
            
            # update joint angles
            current_joints = new_joints
            
            # set joint state in shadow client only during iterations
            if self.use_shadow_client:
                for i, joint_idx in enumerate(self.joint_indices):
                    p.resetJointState(self.shadow_robot_id, joint_idx, current_joints[i], 
                                     physicsClientId=self.shadow_client_id)
            else:
                for i, joint_idx in enumerate(self.joint_indices):
                    p.resetJointState(self.robot_id, joint_idx, current_joints[i])
                
        return current_joints.tolist()
    
    def __del__(self):
        """Clean up shadow client if it exists"""
        if hasattr(self, 'shadow_client_id') and self.shadow_client_id is not None:
            p.disconnect(physicsClientId=self.shadow_client_id)
